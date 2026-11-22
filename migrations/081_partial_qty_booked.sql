-- Migration 081: quantity already sold by partial exits on a position.
-- book_partial() shrinks trades.quantity to what remains, so after a partial the
-- ORIGINAL size (needed for pnl_r's risk basis and for showing the whole trade on the
-- Closed tab) was unrecoverable. partial_qty_booked is the running total sold by
-- partials, written in the same UPDATE as partial_pnl_booked; original size =
-- quantity + partial_qty_booked.
--
-- Semantics note (supersedes 060's "left untouched at final close"): from this
-- migration on, pnl_realized is the WHOLE trade's net P&L (last leg + partial_pnl_booked),
-- added inside TradeRepository.update_pnl. Rows closed before this fix hold last-leg only.

ALTER TABLE trades ADD COLUMN IF NOT EXISTS partial_qty_booked INTEGER NOT NULL DEFAULT 0;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS partial_qty_booked INTEGER NOT NULL DEFAULT 0;
