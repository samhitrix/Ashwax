-- Itemised transaction costs per trade (brokerage, STT, exchange, SEBI, stamp, GST, DP) and the
-- broker whose rate card produced them. charges_breakdown is cumulative: partial exits add to it,
-- the close writes the whole-trade total. Stored P&L semantics are unchanged (pnl_realized net,
-- options realized_pnl gross); the API derives gross/charges/net uniformly.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS charges_breakdown JSONB;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS broker TEXT;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS charges_breakdown JSONB;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS broker TEXT;
ALTER TABLE options_positions ADD COLUMN IF NOT EXISTS charges_breakdown JSONB;
ALTER TABLE options_positions ADD COLUMN IF NOT EXISTS broker TEXT;
ALTER TABLE options_positions ADD COLUMN IF NOT EXISTS charges_taxes NUMERIC(12, 2);
