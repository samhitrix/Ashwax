-- Migration 028: Opportunity model (anti-FOMO distribution tracking)

CREATE TABLE IF NOT EXISTS opportunity_model (
    id               BIGSERIAL PRIMARY KEY,
    symbol           TEXT      NOT NULL,
    opportunity_type TEXT      NOT NULL,
    expected_value   NUMERIC(8, 4),
    confidence       NUMERIC(4, 2),
    regime           TEXT,
    expires_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_opportunity_symbol ON opportunity_model (symbol);
