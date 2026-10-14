-- Migration 044: Options ATM-IV history (Phase 6)
-- Daily ATM implied-vol snapshots per underlying → feeds the IV-percentile gate
-- used by the structure selector and options risk gate.

CREATE TABLE IF NOT EXISTS options_iv_history (
    id            BIGSERIAL    PRIMARY KEY,
    underlying    TEXT         NOT NULL,
    snapshot_date DATE         NOT NULL,
    atm_iv        NUMERIC(8, 5) NOT NULL,
    expiry        DATE,
    days_to_expiry INTEGER,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (underlying, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_options_iv_underlying_date
    ON options_iv_history (underlying, snapshot_date DESC);
