-- Migration 006: Signal capture fields

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS time_of_day_bucket  TEXT,
    ADD COLUMN IF NOT EXISTS candles_since_open   SMALLINT,
    ADD COLUMN IF NOT EXISTS bid_ask_spread_pct   NUMERIC(8, 4);
