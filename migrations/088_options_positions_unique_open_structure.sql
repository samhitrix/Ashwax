-- At most ONE open structure per (underlying, strategy, expiry, paper/live mode).
-- 2026-10-16: two identical NIFTY bull_put_spreads (same expiry, same 22400/22650 strikes) were
-- opened 5 minutes apart because the only guard was a same-direction COUNT cap of 2. This index is
-- the last-resort layer: SellerDesk checks in memory (pre-sizing), under a lock (pre-order) and
-- against this table (pre-order) first, but if all of those were ever bypassed the INSERT of the
-- second row fails here and the desk flattens the structure it just placed.
-- Fails loudly (does not silently skip) if duplicate OPEN rows already exist: close one first.
CREATE UNIQUE INDEX IF NOT EXISTS uq_options_positions_open_structure
    ON options_positions (underlying, strategy_type, expiry, is_paper)
    WHERE status = 'OPEN';
