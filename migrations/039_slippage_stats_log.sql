-- Migration 039: Slippage statistics per bucket (DB truth, JSON is cache)
CREATE TABLE IF NOT EXISTS slippage_stats_log (
    id           BIGSERIAL    PRIMARY KEY,
    order_type   TEXT         NOT NULL,  -- MARKET | LIMIT | SL | SL_M
    time_of_day  TEXT         NOT NULL,  -- morning | mid_morning | lunch | afternoon | pre_close
    adv_bucket   TEXT         NOT NULL,  -- low | mid | high
    avg_slippage NUMERIC(8,4) NOT NULL,
    count        INTEGER      NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_slippage_bucket ON slippage_stats_log (order_type, time_of_day, adv_bucket, created_at DESC);
