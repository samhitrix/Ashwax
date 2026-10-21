-- Migration 088: record which market-data provider produced each candle.
-- NULL = written before provider tagging existed (all Kite-sourced history).
ALTER TABLE ohlcv_candles ADD COLUMN IF NOT EXISTS source TEXT;
