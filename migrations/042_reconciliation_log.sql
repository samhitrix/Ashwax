-- Migration 042: Position reconciliation history (DB truth for Self-Healing tab).
-- One row per reconcile run: broker positions vs DB open trades.

CREATE TABLE IF NOT EXISTS reconciliation_log (
    id               BIGSERIAL   PRIMARY KEY,
    ran_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    db_positions     INTEGER     NOT NULL DEFAULT 0,
    broker_positions INTEGER     NOT NULL DEFAULT 0,
    matched          INTEGER     NOT NULL DEFAULT 0,
    gaps             JSONB,
    gtts_checked     INTEGER     NOT NULL DEFAULT 0,
    gtts_rearmed     INTEGER     NOT NULL DEFAULT 0,
    incidents_raised INTEGER     NOT NULL DEFAULT 0,
    duration_ms      INTEGER     NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_ran_at ON reconciliation_log (ran_at DESC);
