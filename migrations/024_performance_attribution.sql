-- Migration 024: Daily performance attribution (7 dimensions)

CREATE TABLE IF NOT EXISTS performance_attribution (
    id               BIGSERIAL PRIMARY KEY,
    trade_id         BIGINT    REFERENCES trades (id) ON DELETE SET NULL,
    attribution_date DATE      NOT NULL,
    strategy_name    TEXT      NOT NULL,
    regime           TEXT,
    timing_bucket    TEXT,
    signal_quality   NUMERIC(5, 2),
    gross_pnl        NUMERIC(12, 2),
    net_pnl          NUMERIC(12, 2),
    r_multiple       NUMERIC(8, 4),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_perf_attr_date     ON performance_attribution (attribution_date DESC);
CREATE INDEX IF NOT EXISTS idx_perf_attr_strategy ON performance_attribution (strategy_name);
