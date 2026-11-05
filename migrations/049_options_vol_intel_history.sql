-- Migration 049: Seller-desk volatility-intelligence history.
-- Written every ~30 min by src/options/volatility_engine.py. Distinct from the
-- buyer-first 044_options_iv_history (single ATM-IV snapshot) — this carries the
-- full seller-desk vol picture (IVR/IVP/vol-premium/term-structure/skew/GEX).

CREATE TABLE IF NOT EXISTS options_vol_intel_history (
    symbol           TEXT          NOT NULL,        -- NIFTY | BANKNIFTY | FINNIFTY
    date             DATE          NOT NULL,
    timestamp        TIMESTAMPTZ   NOT NULL,
    iv_30d           NUMERIC(8, 5),                 -- 30-day ATM implied vol
    iv_realized_20d  NUMERIC(8, 5),
    iv_realized_30d  NUMERIC(8, 5),
    iv_realized_60d  NUMERIC(8, 5),
    ivr              NUMERIC(6, 3),                 -- IV Rank 0-100
    ivp              NUMERIC(6, 3),                 -- IV Percentile 0-100
    vol_premium      NUMERIC(8, 5),                 -- IV - RV30 (+ = sell, - = buy)
    term_structure   TEXT,                          -- contango | backwardation | flat
    skew_25delta     NUMERIC(8, 5),                 -- put IV - call IV at 25-delta
    gex              NUMERIC(18, 2),                -- gamma exposure (NIFTY/BANKNIFTY)
    confidence       TEXT          NOT NULL DEFAULT 'VIX_PROXY',  -- VIX_PROXY | PARTIAL | FULL
    PRIMARY KEY (symbol, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_options_vol_intel_symbol_date
    ON options_vol_intel_history (symbol, date DESC);
