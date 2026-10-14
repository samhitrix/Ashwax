-- llm_call_log: one row per LLM ORIGINATION call SENT (swing / intraday), written BEFORE
-- the call is made (2026-09-24).
--
-- Why: the per-day call caps (config/global.json llm_swing.max_calls_per_day and
-- llm_intraday.trigger.max_passes_per_day) used to live in process memory / were derived
-- from journal rows written only AFTER a reply. The orchestrator was killed ~30 times on
-- 2026-09-24, each restart reset the counters, and a call killed mid-flight was never
-- journalled -- so the caps did not hold (intraday ran 13 passes on 09-22 and 09-23
-- against a cap of 8; swing re-asked ~23 times with no journal row). A row written before
-- the call survives a kill, so the count is the number of calls actually sent.
--
-- run_date is the IST trading date, supplied by the caller.

CREATE TABLE IF NOT EXISTS llm_call_log (
    id         BIGSERIAL PRIMARY KEY,
    purpose    TEXT        NOT NULL,
    run_date   DATE        NOT NULL,
    called_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note       TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_call_log_purpose_date
    ON llm_call_log (purpose, run_date);

-- One-off backfill: intraday passes already journalled today (IST), so today's cap counts
-- the calls that were really sent before this table existed. (Swing calls made earlier
-- today were never journalled -- they were killed mid-flight -- so they cannot be
-- recovered; swing starts today at 0.)
INSERT INTO llm_call_log (purpose, run_date, called_at, note)
SELECT 'intraday', run_date, run_ts, 'backfill from llm_intraday_journal'
  FROM llm_intraday_journal
 WHERE run_date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date;
