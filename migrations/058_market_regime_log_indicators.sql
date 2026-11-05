-- Migration 058: real ADX/ATR%/suitable-avoid-strategies/historical-accuracy/position-size
-- columns on market_regime_log — previously computed nowhere and hardcoded 0/[] in
-- /api/regime with an explicit "not persisted" comment.

ALTER TABLE market_regime_log
    ADD COLUMN IF NOT EXISTS adx                     NUMERIC(6, 2),
    ADD COLUMN IF NOT EXISTS atr_pct                 NUMERIC(6, 3),
    ADD COLUMN IF NOT EXISTS suitable_strategies      JSONB,
    ADD COLUMN IF NOT EXISTS avoid_strategies          JSONB,
    ADD COLUMN IF NOT EXISTS historical_accuracy      NUMERIC(5, 4),
    ADD COLUMN IF NOT EXISTS position_size_multiplier NUMERIC(4, 2);
