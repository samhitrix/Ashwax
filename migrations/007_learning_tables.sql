-- Migration 007: Learning tables

CREATE TABLE IF NOT EXISTS autopsy_attributions (
    id              BIGSERIAL   PRIMARY KEY,
    trade_id        BIGINT      REFERENCES trades (id) ON DELETE CASCADE,
    axis            TEXT        NOT NULL,
    attribution     TEXT        NOT NULL,
    confidence      NUMERIC(4, 2),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_autopsy_trade_id ON autopsy_attributions (trade_id);

CREATE TABLE IF NOT EXISTS learning_promotions (
    id              BIGSERIAL   PRIMARY KEY,
    strategy_name   TEXT        NOT NULL,
    from_version    TEXT        NOT NULL,
    to_version      TEXT        NOT NULL,
    trigger_reason  TEXT,
    rollback_at     TIMESTAMPTZ,
    promoted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
