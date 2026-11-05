-- Migration 054: Seller-desk daily Greeks P&L attribution (spec §8).
-- Tells you WHERE the day's P&L came from. If theta is the consistent winner and
-- delta is noise, the selling edge is real; if gamma is consistently negative,
-- positions are too close to ATM.

CREATE TABLE IF NOT EXISTS options_pnl_attribution (
    date        DATE          PRIMARY KEY,
    net_delta   NUMERIC(14, 2),
    net_gamma   NUMERIC(14, 2),
    net_theta   NUMERIC(14, 2),
    net_vega    NUMERIC(14, 2),
    delta_pnl   NUMERIC(14, 2),
    gamma_pnl   NUMERIC(14, 2),
    theta_pnl   NUMERIC(14, 2),
    vega_pnl    NUMERIC(14, 2),
    total_pnl   NUMERIC(14, 2),
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
