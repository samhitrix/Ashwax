-- LLM discretionary agent (src/strategies/llm_discretionary/) journal: the
-- persisted "memory" of each day's reasoning pass, read back on subsequent
-- days so the agent can see what it decided and how it turned out. Candidate
-- ideas and their linked agent_trade_id (if any traded) live in candidates_json
-- so outcomes can be joined against trades/paper_trades later; this table never
-- feeds a risk gate directly.
CREATE TABLE IF NOT EXISTS llm_discretionary_journal (
    id                     BIGSERIAL PRIMARY KEY,
    run_date               DATE        NOT NULL,
    market_context_summary JSONB       NOT NULL DEFAULT '{}'::jsonb,
    thesis_text            TEXT,
    candidates_json         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    decision               TEXT        NOT NULL DEFAULT 'no_trade_today',
    llm_model_used         TEXT,
    raw_llm_response       TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_date)
);

CREATE INDEX IF NOT EXISTS idx_llm_discretionary_journal_run_date
    ON llm_discretionary_journal (run_date DESC);
