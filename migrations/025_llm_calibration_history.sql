-- Migration 025: LLM calibration history (weekly Brier score per confidence bucket)

CREATE TABLE IF NOT EXISTS llm_calibration_history (
    id                  BIGSERIAL PRIMARY KEY,
    period_start        DATE      NOT NULL,
    period_end          DATE      NOT NULL,
    model_used          TEXT      NOT NULL,
    confidence_bucket   TEXT      NOT NULL,
    predicted_wr        NUMERIC(4, 2),
    actual_wr           NUMERIC(4, 2),
    trade_count         INTEGER,
    brier_score         NUMERIC(8, 6),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
