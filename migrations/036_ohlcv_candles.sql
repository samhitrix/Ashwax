-- Migration 036: OHLCV candles table for all timeframes

CREATE TABLE IF NOT EXISTS ohlcv_candles (
    id         BIGSERIAL PRIMARY KEY,
    symbol     TEXT         NOT NULL,
    timeframe  TEXT         NOT NULL,
    ts         TIMESTAMPTZ  NOT NULL,
    open       NUMERIC(12,2) NOT NULL,
    high       NUMERIC(12,2) NOT NULL,
    low        NUMERIC(12,2) NOT NULL,
    close      NUMERIC(12,2) NOT NULL,
    volume     BIGINT        NOT NULL DEFAULT 0,
    UNIQUE (symbol, timeframe, ts)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_tf_ts
    ON ohlcv_candles (symbol, timeframe, ts DESC);
