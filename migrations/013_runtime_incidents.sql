-- Migration 013: Runtime incidents with partial unique index

CREATE TABLE IF NOT EXISTS runtime_incidents (
    id                  BIGSERIAL   PRIMARY KEY,
    dedup_key           TEXT        NOT NULL,
    invariant_id        TEXT        NOT NULL,
    severity            TEXT        NOT NULL,
    message             TEXT        NOT NULL,
    remediation_hint    TEXT,
    lifecycle           TEXT        NOT NULL DEFAULT 'OPEN',
    auto_fix_applied    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Only one OPEN incident per dedup_key at a time
CREATE UNIQUE INDEX IF NOT EXISTS uq_incidents_dedup_open
    ON runtime_incidents (dedup_key)
    WHERE lifecycle = 'OPEN';

CREATE INDEX IF NOT EXISTS idx_incidents_lifecycle  ON runtime_incidents (lifecycle);
CREATE INDEX IF NOT EXISTS idx_incidents_created_at ON runtime_incidents (created_at DESC);
