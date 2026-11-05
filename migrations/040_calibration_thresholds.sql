-- Migration 040: Persist LLM calibration thresholds per strategy

CREATE TABLE IF NOT EXISTS calibration_thresholds (
    strategy        TEXT        PRIMARY KEY,
    min_confidence  NUMERIC(5,4) NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
