-- Migration 050: Seller-desk vol-surface snapshots (hourly JSONB).
-- The ONLY legitimate data source for the options backtester (spec §8/§17):
-- never backtest on dates before the system began collecting these.
-- surface_data = {expiry: {strike: {iv, delta, gamma, theta, vega, bid, ask, oi,
--                                   volume, data_quality}}}

CREATE TABLE IF NOT EXISTS options_vol_surface_snapshots (
    symbol        TEXT         NOT NULL,
    timestamp     TIMESTAMPTZ  NOT NULL,
    surface_data  JSONB        NOT NULL,
    PRIMARY KEY (symbol, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_options_vol_surface_symbol_ts
    ON options_vol_surface_snapshots (symbol, timestamp DESC);
