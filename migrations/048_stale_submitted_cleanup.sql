-- Migration 048: auto-TIMED_OUT stale SUBMITTED paper_trades
--
-- Problem this solves: when LTP fetch or SL placement fails after the entry row
-- is already written as SUBMITTED, the row is orphaned forever.  Application code
-- is the only protection and it has failed multiple times (SL network error, partial
-- internet outage).  This migration adds a DB-level safety net that is entirely
-- independent of application code.
--
-- What it does:
--   1. A function cleanup_stale_submitted_paper_trades() that sets order_state →
--      'TIMED_OUT' for any paper_trades row that has been SUBMITTED for more than
--      15 minutes.  15 min > the longest realistic fill time (paper fills are
--      synchronous — any row still SUBMITTED after 15 min is definitely orphaned).
--   2. (Optional) A pg_cron schedule if pg_cron is available.  If not, the launcher
--      calls the function on startup via _run_startup_cleanup() in orchestrator.py.

-- ── 1. Cleanup function ────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION cleanup_stale_submitted_paper_trades(
    stale_after_minutes INT DEFAULT 15
)
RETURNS INT  -- number of rows cleaned up
LANGUAGE plpgsql
AS $$
DECLARE
    _count INT;
BEGIN
    UPDATE paper_trades
       SET order_state  = 'TIMED_OUT',
           exit_reason  = 'STALE_SUBMITTED',
           updated_at   = NOW()
     WHERE order_state  = 'SUBMITTED'
       AND entry_timestamp < NOW() - (stale_after_minutes || ' minutes')::INTERVAL;

    GET DIAGNOSTICS _count = ROW_COUNT;

    IF _count > 0 THEN
        RAISE NOTICE 'cleanup_stale_submitted: marked % rows TIMED_OUT', _count;
    END IF;

    RETURN _count;
END;
$$;

-- ── 2. Schedule via pg_cron if extension is available ─────────────────────────
-- Runs every 5 minutes.  Safe to skip if pg_cron is not installed.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_extension WHERE extname = 'pg_cron'
    ) THEN
        PERFORM cron.schedule(
            'cleanup-stale-submitted-paper-trades',
            '*/5 * * * *',
            'SELECT cleanup_stale_submitted_paper_trades(15)'
        );
        RAISE NOTICE 'pg_cron job scheduled for stale-submitted cleanup';
    ELSE
        RAISE NOTICE 'pg_cron not installed — launcher will call cleanup on startup instead';
    END IF;
END;
$$;
