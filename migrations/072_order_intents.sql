-- Migration 072: durable order-intent ledger (idempotency guard, src/execution/idempotency.py).
-- One row per order attempt key. Purely additive: nothing else reads or writes this table, and
-- the guard fails open (an error here never blocks an order).

CREATE TABLE IF NOT EXISTS order_intents (
    intent_key       TEXT         PRIMARY KEY,   -- sha256(signal_id|symbol|direction|qty|price)[:16]
    agent_trade_id   TEXT         NOT NULL,
    signal_id        TEXT         NOT NULL,
    symbol           TEXT         NOT NULL,
    direction        TEXT         NOT NULL,
    quantity         INTEGER      NOT NULL,
    price            NUMERIC(14, 2),
    is_paper         BOOLEAN      NOT NULL DEFAULT TRUE,
    state            TEXT         NOT NULL DEFAULT 'PENDING',  -- PENDING | SUBMITTED | FAILED
    broker_order_id  TEXT,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_order_intents_updated ON order_intents (updated_at);
