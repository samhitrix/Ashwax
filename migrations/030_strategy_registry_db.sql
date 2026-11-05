-- Migration 030: Strategy registry DB table (agent-created strategies metadata)

CREATE TABLE IF NOT EXISTS strategy_registry_db (
    id                     BIGSERIAL PRIMARY KEY,
    strategy_name          TEXT      NOT NULL UNIQUE,
    source                 TEXT      NOT NULL DEFAULT 'builtin',
    spec_path              TEXT,
    backtest_wr            NUMERIC(5, 2),
    backtest_pf            NUMERIC(8, 4),
    backtest_expectancy    NUMERIC(8, 4),
    backtest_trade_count   INTEGER,
    is_active              BOOLEAN   NOT NULL DEFAULT TRUE,
    health_score           NUMERIC(5, 2),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
