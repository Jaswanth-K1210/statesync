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
    def create_order(self, order_id: str, payment_id: str, total_paise: int,
                     status: str = ..., inventory_units: int = ...) -> None: ...
    def expire_order(self, order_id: str, expected_status: str) -> bool: ...
    def void_order(self, order_id: str) -> bool: ...
    def release_inventory(self, order_id: str) -> bool: ...
    def order_status(self, order_id: str) -> str | None: ...
    def inventory_released(self, order_id: str) -> bool: ...
    def inventory_units(self, order_id: str) -> int: ...
    def snapshot(self) -> dict[str, Any]: ...
    def has(self, repair_key: str) -> bool: ...
    def get(self, repair_key: str) -> dict[str, Any]: ...
    def count(self) -> int: ...

    # There is deliberately no increment/decrement method. A relative update
    # (`stock = stock + n`) is not idempotent: run twice it writes no new row,
    # so a row-count check stays green while the value is silently wrong. The
    # unsafe shape is absent from the interface so it cannot be written by
    # accident. See tests/unit/test_executor_idempotency_class.py.


def _reject_floats(payload: dict[str, Any]) -> None:
    """Constraint 1 holds all the way to the write, not just to the ledger."""
    canonical(payload)


class InMemoryRepairStore:
    def __init__(self) -> None:
        self._repairs: dict[str, dict[str, Any]] = {}
        self._orders_by_payment: dict[str, str] = {}
        self._orders: dict[str, dict[str, Any]] = {}
        self.write_count = 0

    def apply(self, repair_key: str, divergence_key: str, payload: dict[str, Any]) -> None:
        _reject_floats(payload)
        if repair_key in self._repairs:
            raise DuplicateRepairKey(repair_key)
        self._repairs[repair_key] = {"divergence_key": divergence_key, "payload": payload}
        self.write_count += 1

    def create_order(self, order_id: str, payment_id: str, total_paise: int,
                     status: str = "confirmed", inventory_units: int = 0) -> None:
        if not isinstance(total_paise, int) or isinstance(total_paise, bool):
            raise TypeError("total_paise must be integer paise")
        if payment_id in self._orders_by_payment:
            raise DuplicateRepairKey(payment_id)
        self._orders_by_payment[payment_id] = order_id
        self._orders[order_id] = {
            "payment_id": payment_id, "total_paise": total_paise,
            "status": status, "inventory_units": inventory_units,
            "inventory_released": False,
        }
        self.write_count += 1

    # ── guarded updates ─────────────────────────────────────────────────────
    # Each returns True only if it changed something. The second call matches
    # nothing and returns False, which is what makes the repair replay-safe
    # without depending on the lease or the unique constraint above it.

    def expire_order(self, order_id: str, expected_status: str = "pending") -> bool:
        order = self._orders.get(order_id)
        if order is None or order["status"] != expected_status:
            return False
        order["status"] = "expired"
        return True

    def void_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order is None or order["status"] == "voided":
            return False
        order["status"] = "voided"
        return True

    def release_inventory(self, order_id: str) -> bool:
        """Absolute assignment of a flag, never an increment of a count."""
        order = self._orders.get(order_id)
        if order is None or order["inventory_released"]:
            return False
        order["inventory_released"] = True
        return True

    def order_status(self, order_id: str) -> str | None:
        order = self._orders.get(order_id)
        return str(order["status"]) if order else None

    def inventory_released(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        return bool(order["inventory_released"]) if order else False

    def inventory_units(self, order_id: str) -> int:
        order = self._orders.get(order_id)
        return int(order["inventory_units"]) if order else 0

    def snapshot(self) -> dict[str, Any]:
        """Full observable state, for asserting a second run changed nothing."""
        return {
            "repairs": {k: dict(v) for k, v in self._repairs.items()},
            "orders": {k: dict(v) for k, v in self._orders.items()},
            "orders_by_payment": dict(self._orders_by_payment),
        }

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
