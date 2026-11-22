-- Migration 045: Options structures placed by the agent (Phase 6)
-- A structure is a multi-leg defined-risk position. Equity trades stay in the
-- `trades` table (its CHECK domains are equity-shaped); options live here.

CREATE TABLE IF NOT EXISTS options_structures (
    id              BIGSERIAL    PRIMARY KEY,
    structure_id    TEXT         NOT NULL UNIQUE,   -- "OPT-..."
    signal_id       TEXT         NOT NULL,
    symbol          TEXT         NOT NULL,          -- underlying, "NSE:RELIANCE"
    structure_type  TEXT         NOT NULL,          -- bull_call_spread | long_call | protective_put
    product         TEXT         NOT NULL,
    legs            JSONB        NOT NULL,           -- [{tradingsymbol, type, strike, direction, lots, premium, delta}]
    premium_paid    NUMERIC(12, 2) NOT NULL,        -- net debit (capital at risk)
    max_loss        NUMERIC(12, 2) NOT NULL,
    max_profit      NUMERIC(12, 2) NOT NULL,
    rr              NUMERIC(8, 4),
    expected_value  NUMERIC(12, 2),
    net_delta       NUMERIC(8, 4),
    leg_order_ids   JSONB,
    sl_order_id     TEXT,
    status          TEXT         NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED | UNWOUND
    reasoning       TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_options_structures_symbol ON options_structures (symbol);
CREATE INDEX IF NOT EXISTS idx_options_structures_status ON options_structures (status);
