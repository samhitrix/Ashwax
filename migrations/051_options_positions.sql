-- Migration 051: Seller-desk multi-leg options positions (seller-first).
-- One row per structure (Iron Condor, credit spread, ...). The lot_size used at
-- ENTRY is frozen here; new lot-size revisions apply to NEW trades only (spec §14).
-- Greeks-at-entry are stored so risk math survives chain-data gaps.

CREATE TABLE IF NOT EXISTS options_positions (
    position_id        TEXT          PRIMARY KEY,    -- "OPT-..."
    underlying         TEXT          NOT NULL,       -- NIFTY | BANKNIFTY | FINNIFTY
    strategy_type      TEXT          NOT NULL,       -- bull_put_spread | bear_call_spread | iron_condor | ...
    status             TEXT          NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED | DEGRADED | EMERGENCY_CLOSED
    legs               JSONB         NOT NULL,       -- [{tradingsymbol, strike, opt_type, direction, lots, entry_price, delta}]
    expiry             DATE          NOT NULL,
    dte_at_entry       INTEGER       NOT NULL,
    lot_size_at_entry  INTEGER       NOT NULL,
    net_credit         NUMERIC(12, 2),               -- + credit received / - debit paid (book-walked)
    max_loss           NUMERIC(12, 2) NOT NULL,
    max_profit         NUMERIC(12, 2) NOT NULL,
    entry_margin       NUMERIC(14, 2),
    entry_delta        NUMERIC(10, 4),
    entry_gamma        NUMERIC(14, 6),
    entry_theta        NUMERIC(12, 4),
    entry_vega         NUMERIC(12, 4),
    adjustments_count  INTEGER       NOT NULL DEFAULT 0,
    realized_pnl       NUMERIC(12, 2),
    exit_reason        TEXT,
    opened_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    closed_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_options_positions_status ON options_positions (status);
CREATE INDEX IF NOT EXISTS idx_options_positions_underlying_expiry
    ON options_positions (underlying, expiry);
