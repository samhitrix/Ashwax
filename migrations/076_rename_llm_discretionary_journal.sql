-- Rename table llm_discretionary_journal -> llm_swing_journal (2026-09-22),
-- following the strategy rename in 075_rename_llm_discretionary_agent.sql.
-- Idempotent: a no-op once the table has already been renamed.

ALTER TABLE IF EXISTS llm_discretionary_journal RENAME TO llm_swing_journal;
ALTER INDEX IF EXISTS idx_llm_discretionary_journal_run_date RENAME TO idx_llm_swing_journal_run_date;
