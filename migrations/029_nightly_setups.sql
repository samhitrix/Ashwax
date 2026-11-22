-- Migration 029: Nightly setups (scanner output for next trading day)

CREATE TABLE IF NOT EXISTS nightly_setups (
    id               BIGSERIAL PRIMARY KEY,
    setup_date       DATE      NOT NULL,
    symbol           TEXT      NOT NULL,
    strategy_name    TEXT      NOT NULL,
    quality_score    NUMERIC(5, 2),
    entry_zone_low   NUMERIC(12, 2),
    entry_zone_high  NUMERIC(12, 2),
    sl_price         NUMERIC(12, 2),
    target_price     NUMERIC(12, 2),
    notes            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (setup_date, symbol, strategy_name)
);

CREATE INDEX IF NOT EXISTS idx_nightly_setups_date ON nightly_setups (setup_date DESC);
