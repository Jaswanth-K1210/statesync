"""Postgres persistence for the audit ledger.

The application role holds SELECT and INSERT only (see `migrations/001`), so
`LedgerStore` has no update or delete path to offer. Verification reloads the
whole chain and walks it — a full walk at the start of every run, because at
these volumes it costs milliseconds and spot-checking would be a false economy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from statesync.config import ADMIN_DSN, APP_DSN, MIGRATIONS_DIR
from statesync.ledger.chain import Ledger, LedgerEntry

__all__ = ["LedgerStore", "apply_migrations"]

_COLUMNS = (
    "seq, prev_hash, hash, event_type, actor, divergence_key, repair_key, created_at, payload"
)


def apply_migrations(dsn: str = ADMIN_DSN, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every .sql file in order, as the owning role. Idempotent."""
    applied: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        for path in sorted(migrations_dir.glob("*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))
            applied.append(path.name)
    return applied


class LedgerStore:
    def __init__(self, dsn: str = APP_DSN) -> None:
        self.dsn = dsn

    def append(self, entry: LedgerEntry) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                f"INSERT INTO ledger_entries ({_COLUMNS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    entry.seq, entry.prev_hash, entry.hash, entry.event_type, entry.actor,
                    entry.divergence_key, entry.repair_key, entry.created_at,
                    Jsonb(entry.payload),
                ),
            )

    def append_all(self, entries: list[LedgerEntry]) -> None:
        for entry in entries:
            self.append(entry)

    def load(self) -> list[LedgerEntry]:
        with psycopg.connect(self.dsn) as conn:
            rows: list[Any] = conn.execute(
                f"SELECT {_COLUMNS} FROM ledger_entries ORDER BY seq"
            ).fetchall()
        return [
            LedgerEntry(
                seq=r[0], prev_hash=r[1], hash=r[2], event_type=r[3], actor=r[4],
                divergence_key=r[5], repair_key=r[6], created_at=r[7], payload=r[8],
            )
            for r in rows
        ]

    def verify(self) -> tuple[bool, int | None]:
        """Reload the persisted chain and walk it end to end."""
        return Ledger(entries=self.load()).verify()

    def count(self) -> int:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute("SELECT count(*) FROM ledger_entries").fetchone()
        return int(row[0]) if row else 0
