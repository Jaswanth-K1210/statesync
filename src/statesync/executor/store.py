"""Where repairs land, and the constraint that makes them idempotent.

**Layer 1 of two.** Every repair writes with a deterministic key. If Redis is
wiped and every repair is attempted again, the database rejects the duplicates.
The guarantee lives in the schema, where it cannot be lost — Redis (layer 2,
`policy/idempotency.py`) is an optimisation on top, never the thing being
relied on.

Two production implementations against one contract:

* `InMemoryRepairStore` — what the eval runs against, in-memory like the rest
  of the reconciler.
* `PostgresRepairStore` — carries the constraint in the schema.

They are tested against the same suite, so the fast one cannot drift from the
real one.
"""

from __future__ import annotations

from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from statesync.config import APP_DSN
from statesync.ledger.canonical import canonical

__all__ = [
    "DuplicateRepairKey", "InMemoryRepairStore", "PostgresRepairStore", "RepairStore",
]


class DuplicateRepairKey(Exception):
    """This repair already happened. Raised by both implementations so the
    executor catches one type regardless of where the write went."""


class RepairStore(Protocol):
    write_count: int
    """Rows landed by this process — not repairs. One repair may write more
    than one row (an order plus its repair record), and the idempotency claim
    is about rows: a second run must add none of them."""

    def apply(self, repair_key: str, divergence_key: str, payload: dict[str, Any]) -> None: ...
    def create_order(self, order_id: str, payment_id: str, total_paise: int) -> None: ...
    def has(self, repair_key: str) -> bool: ...
    def get(self, repair_key: str) -> dict[str, Any]: ...
    def count(self) -> int: ...


def _reject_floats(payload: dict[str, Any]) -> None:
    """Constraint 1 holds all the way to the write, not just to the ledger."""
    canonical(payload)


class InMemoryRepairStore:
    def __init__(self) -> None:
        self._repairs: dict[str, dict[str, Any]] = {}
        self._orders_by_payment: dict[str, str] = {}
        self.write_count = 0

    def apply(self, repair_key: str, divergence_key: str, payload: dict[str, Any]) -> None:
        _reject_floats(payload)
        if repair_key in self._repairs:
            raise DuplicateRepairKey(repair_key)
        self._repairs[repair_key] = {"divergence_key": divergence_key, "payload": payload}
        self.write_count += 1

    def create_order(self, order_id: str, payment_id: str, total_paise: int) -> None:
        if not isinstance(total_paise, int) or isinstance(total_paise, bool):
            raise TypeError("total_paise must be integer paise")
        if payment_id in self._orders_by_payment:
            raise DuplicateRepairKey(payment_id)
        self._orders_by_payment[payment_id] = order_id
        self.write_count += 1

    def has(self, repair_key: str) -> bool:
        return repair_key in self._repairs

    def get(self, repair_key: str) -> dict[str, Any]:
        return self._repairs[repair_key]

    def count(self) -> int:
        return len(self._repairs)


class PostgresRepairStore:
    """The real guarantee. `write_count` counts rows this process landed."""

    def __init__(self, dsn: str = APP_DSN) -> None:
        self.dsn = dsn
        self.write_count = 0

    def apply(self, repair_key: str, divergence_key: str, payload: dict[str, Any]) -> None:
        _reject_floats(payload)
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(
                    "INSERT INTO repairs (repair_key, divergence_key, payload) "
                    "VALUES (%s, %s, %s)",
                    (repair_key, divergence_key, Jsonb(payload)),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateRepairKey(repair_key) from exc
        self.write_count += 1

    def create_order(self, order_id: str, payment_id: str, total_paise: int) -> None:
        if not isinstance(total_paise, int) or isinstance(total_paise, bool):
            raise TypeError("total_paise must be integer paise")
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(
                    "INSERT INTO orders (order_id, payment_id, total_paise) VALUES (%s, %s, %s)",
                    (order_id, payment_id, total_paise),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateRepairKey(payment_id) from exc
        self.write_count += 1

    def has(self, repair_key: str) -> bool:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT 1 FROM repairs WHERE repair_key = %s", (repair_key,)
            ).fetchone()
        return row is not None

    def get(self, repair_key: str) -> dict[str, Any]:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT divergence_key, payload FROM repairs WHERE repair_key = %s",
                (repair_key,),
            ).fetchone()
        if row is None:
            raise KeyError(repair_key)
        return {"divergence_key": row[0], "payload": row[1]}

    def count(self) -> int:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute("SELECT count(*) FROM repairs").fetchone()
        return int(row[0]) if row else 0
