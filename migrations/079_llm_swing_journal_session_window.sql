-- llm_swing_journal: tag each row with whether it actually ran during market hours
-- or after close (2026-09-23).
--
-- Earlier incidents (2026-09-22) show the daily pass running repeatedly after market
-- close on restarts -- worthless for same-day entry, but indistinguishable from a
-- normal pass without checking created_at against session times by hand. Computed
-- once at insert time from src.market.sessions.regular_open()/regular_close() against
-- that row's real wall-clock time, so it stays correct even if market hours config
-- changes later, and so a restart-driven after-hours pass is visible and queryable
-- rather than hidden in a timestamp.
--
-- Additive and safe: existing rows default to NULL (unknown -- they predate this
-- column and were not computed at the time).

ALTER TABLE llm_swing_journal
    ADD COLUMN IF NOT EXISTS session_window TEXT;

CREATE INDEX IF NOT EXISTS idx_llm_swing_journal_run_date_decision
    ON llm_swing_journal (run_date, decision);
