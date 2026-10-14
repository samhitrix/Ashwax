-- LLM intraday agent (src/strategies/llm_intraday/) journal: one row per
-- scheduled reasoning pass (several per session). Records the snapshot the LLM
-- saw, its thesis and the validated ideas it published. Trade outcomes are
-- joined from paper_trades (strategy_name = 'llm_intraday_agent') rather than
-- copied here. This table never feeds a risk gate.
CREATE TABLE IF NOT EXISTS llm_intraday_journal (
    id               BIGSERIAL PRIMARY KEY,
    run_ts           TIMESTAMPTZ NOT NULL,
    run_date         DATE        NOT NULL,
    market_context   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    thesis_text      TEXT,
    ideas_json       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    decision         TEXT        NOT NULL DEFAULT 'no_ideas',
    llm_model_used   TEXT,
    raw_llm_response TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_ts)
);

CREATE INDEX IF NOT EXISTS idx_llm_intraday_journal_run_date
    ON llm_intraday_journal (run_date DESC);
