-- Migration 002: Ticks table (no TimescaleDB dependency)

CREATE TABLE IF NOT EXISTS ticks (
    ts      TIMESTAMPTZ    NOT NULL,
    symbol  TEXT           NOT NULL,
    ltp     NUMERIC(12, 2) NOT NULL,
    volume  BIGINT,
    bid     NUMERIC(12, 2),
    ask     NUMERIC(12, 2),
    PRIMARY KEY (ts, symbol)
);

CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON ticks (symbol, ts DESC);
