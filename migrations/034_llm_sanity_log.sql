-- Migration 034: LLM sanity watchdog log

CREATE TABLE IF NOT EXISTS llm_sanity_log (
    id                       BIGSERIAL PRIMARY KEY,
    window_start             TIMESTAMPTZ NOT NULL,
    window_end               TIMESTAMPTZ NOT NULL,
    approved_wr              NUMERIC(5, 2),
    rejected_would_be_wr     NUMERIC(5, 2),
    bypass_triggered         BOOLEAN     NOT NULL DEFAULT FALSE,
    trade_count              INTEGER,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
