-- Migration 001: Core tables

CREATE TABLE IF NOT EXISTS trades (
    id                      BIGSERIAL   PRIMARY KEY,
    agent_trade_id          TEXT        NOT NULL UNIQUE,
    broker_order_id         TEXT,
    exchange_order_id       TEXT,
    broker_trade_id         TEXT,
    broker_order_tag        TEXT,
    symbol                  TEXT        NOT NULL,
    exchange                TEXT        NOT NULL DEFAULT 'NSE',
    direction               TEXT        NOT NULL CHECK (direction IN ('long', 'short')),
    quantity                INTEGER     NOT NULL,
    order_state             TEXT        NOT NULL DEFAULT 'PENDING_SUBMIT',
    intended_price          NUMERIC(12, 2),
    fill_price              NUMERIC(12, 2),
    fill_timestamp          TIMESTAMPTZ,
    exit_price              NUMERIC(12, 2),
    exit_timestamp          TIMESTAMPTZ,
    initial_sl              NUMERIC(12, 2),
    current_sl              NUMERIC(12, 2),
    initial_target          NUMERIC(12, 2),
    current_target          NUMERIC(12, 2),
    signal_id               BIGINT,
    strategy_name           TEXT,
    trade_type              TEXT        NOT NULL DEFAULT 'NORMAL'
                                CHECK (trade_type IN ('NORMAL', 'NEWS_DRIVEN')),
    regime_at_entry         TEXT,
    rs_rank_at_entry        SMALLINT,
    setup_quality_score     NUMERIC(5, 2),
    pnl_realized            NUMERIC(12, 2),
    pnl_unrealized          NUMERIC(12, 2),
    charges_taxes           NUMERIC(12, 2),
    actual_risk_rupees      NUMERIC(12, 2),
    actual_rr_ratio         NUMERIC(8, 4),
    scale_1_qty             INTEGER,
    scale_1_target          NUMERIC(12, 2),
    scale_2_qty             INTEGER,
    scale_2_target          NUMERIC(12, 2),
    scale_3_qty             INTEGER,
    entry_timestamp         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol      ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_order_state ON trades (order_state);
CREATE INDEX IF NOT EXISTS idx_trades_entry_ts    ON trades (entry_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_strategy    ON trades (strategy_name);

CREATE TABLE IF NOT EXISTS signals (
    id              BIGSERIAL   PRIMARY KEY,
    symbol          TEXT        NOT NULL,
    strategy_name   TEXT        NOT NULL,
    direction       TEXT        NOT NULL CHECK (direction IN ('long', 'short')),
    entry_price     NUMERIC(12, 2),
    sl_price        NUMERIC(12, 2),
    target_price    NUMERIC(12, 2),
    quality_score   NUMERIC(5, 2),
    gate_results    JSONB,
    llm_confidence  NUMERIC(4, 2),
    llm_reasoning   TEXT,
    disposition     TEXT        NOT NULL DEFAULT 'PENDING',
    trade_id        BIGINT      REFERENCES trades (id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol     ON signals (symbol);
CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals (created_at DESC);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT        PRIMARY KEY,
    result      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS watchlist (
    id          BIGSERIAL   PRIMARY KEY,
    symbol      TEXT        NOT NULL,
    exchange    TEXT        NOT NULL DEFAULT 'NSE',
    universe    TEXT        NOT NULL DEFAULT 'NIFTY500',
    active      BOOLEAN     NOT NULL DEFAULT TRUE,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, exchange)
);

CREATE TABLE IF NOT EXISTS journal (
    id          BIGSERIAL   PRIMARY KEY,
    entry_date  DATE        NOT NULL,
    content     TEXT        NOT NULL,
    tags        TEXT[],
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
