-- Migration 060: track profit already realized from partial exits on a still-open
-- position. book_partial() (order_lifecycle.py) sells a fraction of a position
-- and keeps monitoring the rest — that realized P&L was never recorded anywhere:
-- pnl_realized/update_pnl are close-only (order_state='CLOSED'), so a partial sale
-- left no trace of what it actually banked. partial_pnl_booked is a running total,
-- separate from pnl_realized, updated on each partial and left untouched at final
-- close (that P&L still belongs to pnl_realized as before).

ALTER TABLE trades ADD COLUMN IF NOT EXISTS partial_pnl_booked DECIMAL(12, 2) NOT NULL DEFAULT 0;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS partial_pnl_booked DECIMAL(12, 2) NOT NULL DEFAULT 0;
