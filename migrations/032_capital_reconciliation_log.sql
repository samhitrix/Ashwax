-- Migration 032: Capital reconciliation log (daily broker vs DB drift)

CREATE TABLE IF NOT EXISTS capital_reconciliation_log (
    id              BIGSERIAL PRIMARY KEY,
    recon_date      DATE      NOT NULL,
    db_capital      NUMERIC(12, 2) NOT NULL,
    broker_capital  NUMERIC(12, 2) NOT NULL,
    drift_rupees    NUMERIC(12, 2) NOT NULL,
    action_taken    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_capital_recon_date ON capital_reconciliation_log (recon_date DESC);
