-- Migration 035: Tax audit trail — append-only, 8-year SEBI/tax retention

CREATE TABLE IF NOT EXISTS tax_audit_trail (
    id          BIGSERIAL PRIMARY KEY,
    event_type  TEXT      NOT NULL,
    event_data  JSONB     NOT NULL,
    trade_id    BIGINT,
    user_id     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tax_audit_created_at ON tax_audit_trail (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tax_audit_event_type ON tax_audit_trail (event_type);
CREATE INDEX IF NOT EXISTS idx_tax_audit_trade_id   ON tax_audit_trail (trade_id);
