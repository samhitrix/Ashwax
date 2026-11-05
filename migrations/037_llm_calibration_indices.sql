-- Migration 037: Add missing indices to llm_calibration_history (spec §8.9)
-- Weekly queries filter by period_start/period_end and model_used.

CREATE INDEX IF NOT EXISTS idx_llm_cal_period
    ON llm_calibration_history (period_start DESC, period_end DESC);

CREATE INDEX IF NOT EXISTS idx_llm_cal_model
    ON llm_calibration_history (model_used, period_start DESC);
