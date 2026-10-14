-- Layer 6 (spec §9) Promotion Engine persistence. Previously PromotionEngine
-- was constructed fresh, in-memory only, every time _evolution_loop started —
-- a promotion's rollback tracking (post-promotion trade count, win rate) was
-- silently lost on every launcher restart. These two tables make promotion
-- and its 20-trade rollback evaluation restart-safe.
CREATE TABLE IF NOT EXISTS promotion_records (
    strategy_name         TEXT        PRIMARY KEY,
    promoted_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    param_snapshot        JSONB       NOT NULL,
    prior_param_snapshot  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    baseline_wr           NUMERIC(5,4) NOT NULL,
    rolled_back           BOOLEAN     NOT NULL DEFAULT FALSE,
    rolled_back_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS promotion_trade_log (
    id              BIGSERIAL   PRIMARY KEY,
    strategy_name   TEXT        NOT NULL,
    pnl_r           NUMERIC(8,4) NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_promotion_trade_log_strategy
    ON promotion_trade_log (strategy_name, recorded_at DESC);
