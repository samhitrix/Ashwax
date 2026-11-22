-- Migration 021: Market regime log

CREATE TABLE IF NOT EXISTS market_regime_log (
    id           BIGSERIAL   PRIMARY KEY,
    regime       TEXT        NOT NULL,
    vix          NUMERIC(8, 4),
    nifty_ma50   NUMERIC(12, 2),
    nifty_ma200  NUMERIC(12, 2),
    breadth_pct  NUMERIC(5, 2),
    confidence   NUMERIC(4, 2),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_regime_log_created_at ON market_regime_log (created_at DESC);
