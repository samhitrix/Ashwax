-- Migration 084: make `signals` the decision journal it was designed to be, and link trades to it.
--
-- Why: `signals` (symbol, strategy, entry/SL/target, gate_results, disposition ...) existed but had
-- ZERO writers, and trades.signal_id (BIGINT) was NULL on all 473 rows, so no trade could be traced
-- to the signal that created it and a signal that was approved but never became a trade was
-- invisible (2026-09-28). Every signal now passes crm.evaluate(); the CRM's decision sink writes one
-- row per evaluation. Trades carry the signal's string id in `signal_ref`.

ALTER TABLE signals ADD COLUMN IF NOT EXISTS signal_id         TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS is_paper          BOOLEAN;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS segment           TEXT;      -- intraday | swing | options
ALTER TABLE signals ADD COLUMN IF NOT EXISTS asset_class       TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS intended_quantity INTEGER;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS final_quantity    INTEGER;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS blocking_gate     TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS blocking_reason   TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS regime            TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS risk_rupees       NUMERIC;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS stop_loss_pct     NUMERIC;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS target_pct        NUMERIC;

CREATE INDEX IF NOT EXISTS idx_signals_signal_id ON signals (signal_id);
CREATE INDEX IF NOT EXISTS idx_signals_created   ON signals (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_strategy  ON signals (strategy_name, created_at DESC);

ALTER TABLE trades       ADD COLUMN IF NOT EXISTS signal_ref TEXT;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS signal_ref TEXT;
CREATE INDEX IF NOT EXISTS idx_trades_signal_ref       ON trades (signal_ref)       WHERE signal_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_trades_signal_ref ON paper_trades (signal_ref) WHERE signal_ref IS NOT NULL;

-- One row per signal with what became of it:
--   REJECTED_BY_CRM       a gate blocked it (blocking_gate / blocking_reason say which and why)
--   APPROVED_NOT_EXECUTED CRM approved it but no trade row exists for it (refused/expired/never placed)
--   EXECUTED              a trade row carries its signal_ref
CREATE OR REPLACE VIEW signal_outcomes AS
SELECT s.id, s.created_at, s.signal_id, s.symbol, s.strategy_name, s.direction, s.segment,
       s.asset_class, s.is_paper, s.entry_price, s.sl_price, s.target_price,
       s.intended_quantity, s.final_quantity, s.disposition, s.blocking_gate, s.blocking_reason,
       s.gate_results, x.agent_trade_id, x.trade_is_paper, x.order_state, x.pnl_realized,
       CASE WHEN s.disposition <> 'APPROVED' THEN 'REJECTED_BY_CRM'
            WHEN x.agent_trade_id IS NULL      THEN 'APPROVED_NOT_EXECUTED'
            ELSE 'EXECUTED' END AS outcome
  FROM signals s
  LEFT JOIN LATERAL (
        SELECT agent_trade_id, false AS trade_is_paper, order_state, pnl_realized
          FROM trades WHERE signal_ref = s.signal_id
        UNION ALL
        SELECT agent_trade_id, true, order_state, pnl_realized
          FROM paper_trades WHERE signal_ref = s.signal_id
        LIMIT 1) x ON TRUE;
