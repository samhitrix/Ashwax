-- Migration 086: daily baseline snapshot of each open option position's
-- unrealized P&L, so the Options Trading dashboard can show "Today's P&L"
-- (today's move only) separately from "Total P&L" (since entry, however
-- many days ago). Without a stored baseline there is nothing to diff
-- against for a position opened on an earlier day.
--
-- One row per (position_id, snapshot_date): captured once near market open
-- each trading day for every still-OPEN position (paper + live). A position
-- opened intraday today has no row for today -- its Today's P&L is then
-- just its current P&L (the whole thing happened today).

CREATE TABLE IF NOT EXISTS options_pnl_daily_snapshot (
    position_id TEXT NOT NULL REFERENCES options_positions(position_id),
    snapshot_date DATE NOT NULL,
    unrealized_pnl NUMERIC(12, 2) NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (position_id, snapshot_date)
);
