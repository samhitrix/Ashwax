-- Migration 032: record WHY a paper trade closed (SL_HIT / TARGET_HIT / REVERSAL / EOD_SQUAREOFF …)
-- Surfaced in the dashboard Paper Trades tab. NULL for rows closed before this column existed.

ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS exit_reason TEXT;
