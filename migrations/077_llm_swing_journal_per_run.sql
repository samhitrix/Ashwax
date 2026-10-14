-- llm_swing_journal: keep EVERY reasoning pass, not one row per calendar day (2026-09-23).
--
-- The table was created with UNIQUE (run_date) and written with ON CONFLICT (run_date)
-- DO UPDATE, so repeated passes on the same day overwrote each other. That is not a
-- theoretical loss: on 2026-09-22 the agent ran five times (19:53-21:54, once per
-- restart) and produced three "no_trade_today" verdicts from gemini followed by two
-- ACUTAAS proposals from groq. Only the last survived. The raw responses that would
-- have shown WHY the model declined three times were destroyed by the next run.
--
-- That evidence is exactly what is needed to answer "is this agent too conservative,
-- or is something upstream starving it?" -- and to compare models against each other,
-- since the overwriting run was frequently a DIFFERENT provider than the one it erased.
--
-- Safe and reversible: dropping a UNIQUE constraint never rejects existing rows, and
-- the per-day index is kept for the recent_runs() lookup.

ALTER TABLE llm_swing_journal DROP CONSTRAINT IF EXISTS llm_discretionary_journal_run_date_key;
ALTER TABLE llm_swing_journal DROP CONSTRAINT IF EXISTS llm_swing_journal_run_date_key;

-- Ordering within a day now matters, so recent_runs() can return the latest passes
-- rather than an arbitrary row per date.
CREATE INDEX IF NOT EXISTS idx_llm_swing_journal_run_date_created
    ON llm_swing_journal (run_date DESC, created_at DESC);
