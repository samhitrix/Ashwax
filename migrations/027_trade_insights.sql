-- Migration 027: Trade insights — Causal Trade Memory

CREATE TABLE IF NOT EXISTS trade_insights (
    id                          BIGSERIAL PRIMARY KEY,
    trade_id                    BIGINT    REFERENCES trades (id) ON DELETE CASCADE,
    was_signal_valid            BOOLEAN,
    failure_was_environmental   BOOLEAN,
    what_i_would_do_differently TEXT,
    generalizable_insight       TEXT,
    confidence_in_insight       NUMERIC(4, 2),
    applicable_when             TEXT,
    regime_at_trade             TEXT,
    sector_at_trade             TEXT,
    llm_model_used              TEXT,
    raw_llm_response            TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trade_insights_trade_id ON trade_insights (trade_id);
