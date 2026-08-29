-- Layer 1 of two-layer idempotency: the constraint that cannot be flushed.
--
-- Redis is a cache. It can be flushed, evicted or partitioned, and it must
-- never be the only thing standing between the system and a double repair.
-- These constraints live in the schema, where the guarantee cannot be lost.

CREATE TABLE IF NOT EXISTS repairs (
    repair_key      TEXT        PRIMARY KEY,
    divergence_key  TEXT        NOT NULL,
    payload         JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS repairs_divergence_key_idx ON repairs (divergence_key);

-- The merchant's order store. `payment_id` is unique: a repair that creates an
-- order for a captured payment can be attempted any number of times and only
-- the first one lands.
CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT        PRIMARY KEY,
    payment_id   TEXT        UNIQUE,
    total_paise  BIGINT      NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'confirmed',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The audit ledger carries the same guarantee: one repair, one entry.
CREATE UNIQUE INDEX IF NOT EXISTS ledger_entries_repair_key_uniq
    ON ledger_entries (repair_key) WHERE repair_key IS NOT NULL;

REVOKE ALL ON repairs, orders FROM statesync_app;
GRANT SELECT, INSERT ON repairs, orders TO statesync_app;
-- UPDATE and DELETE are deliberately never granted here either.
