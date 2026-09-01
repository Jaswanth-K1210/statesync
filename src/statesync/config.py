"""Project-wide constants. Every one of these is referenced by a test."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

__all__ = [
    "ADMIN_DSN", "APP_DSN", "BASE_TIME", "CACHE_DIR", "LEASE_SECONDS",
    "MIGRATIONS_DIR", "ORDER_TIMEOUT", "PROJECT_ROOT", "REDIS_URL", "SEED",
    "STALENESS_WINDOW",
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SEED = 20260905
"""Threads through every generator and every shuffle. Constraint 3."""

BASE_TIME = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
"""Anchor for all generated timestamps. Deliberately not `now()` — wall-clock
time in generated data would make `make eval` non-reproducible."""

STALENESS_WINDOW = timedelta(minutes=15)
"""No payment is reconciled until it has been terminal this long."""

LEASE_SECONDS = 300
"""Redis repair lease TTL. An expired lease means a worker died mid-repair."""

CACHE_DIR = PROJECT_ROOT / "llm_cache"
"""Committed to the repo. The demo must not depend on a network call."""

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

ADMIN_DSN = os.getenv(
    "STATESYNC_ADMIN_DSN", "postgresql://statesync:statesync@localhost:55432/statesync"
)
"""Owns the schema and runs migrations. Never used by the running system."""

APP_DSN = os.getenv(
    "STATESYNC_APP_DSN", "postgresql://statesync_app:statesync_app@localhost:55432/statesync"
)
"""What the reconciler connects as: SELECT and INSERT on the ledger, nothing
else. UPDATE and DELETE are not granted, so the audit trail is append-only by
permission rather than by good intentions."""

REDIS_URL = os.getenv("STATESYNC_REDIS_URL", "redis://localhost:56379/0")

ORDER_TIMEOUT = timedelta(hours=1)
"""An order whose payment has not captured within this is ORDER_NO_CAPTURE.

Distinct from STALENESS_WINDOW: that one gates *terminal* payments, and an
abandoned order's payment never reaches a terminal state at all, so it would
otherwise never become eligible for reconciliation."""
