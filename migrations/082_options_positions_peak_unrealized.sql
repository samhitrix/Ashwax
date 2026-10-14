-- Migration 082: high-water mark of unrealized P&L (rupees) per options position.
-- Feeds the debit-spread breakeven lock (position_manager.check_breakeven_lock): once a
-- debit spread has been up debit_breakeven_lock_arm_pct of its debit, a return to zero
-- exits. In-memory only, a launcher restart would forget the peak and disarm the lock.
-- NULL = never observed with complete leg prices (all pre-082 rows).

ALTER TABLE options_positions
    ADD COLUMN IF NOT EXISTS peak_unrealized_pnl NUMERIC(14, 2);
