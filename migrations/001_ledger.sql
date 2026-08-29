-- The audit ledger. INSERT-only by grant, not by convention.
--
-- The hash chain detects tampering after the fact; this role separation is
-- what stops the application from being able to tamper at all. A reconciler
-- that can rewrite its own audit trail is not auditable.

CREATE TABLE IF NOT EXISTS ledger_entries (
    seq             BIGINT      PRIMARY KEY,
    prev_hash       CHAR(64)    NOT NULL,
    hash            CHAR(64)    NOT NULL UNIQUE,
    event_type      TEXT        NOT NULL,
    actor           TEXT        NOT NULL,
    divergence_key  TEXT,
    repair_key      TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    payload         JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS ledger_entries_event_type_idx ON ledger_entries (event_type);
CREATE INDEX IF NOT EXISTS ledger_entries_divergence_key_idx ON ledger_entries (divergence_key);

-- The application connects as this role and can only ever add to the chain.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'statesync_app') THEN
        CREATE ROLE statesync_app LOGIN PASSWORD 'statesync_app';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE statesync TO statesync_app;
GRANT USAGE ON SCHEMA public TO statesync_app;

REVOKE ALL ON ledger_entries FROM statesync_app;
GRANT SELECT, INSERT ON ledger_entries TO statesync_app;
-- UPDATE and DELETE are deliberately never granted.
