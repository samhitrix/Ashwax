-- Migration 018: Order lifecycle fields (GTT ids, slippage, retry, rejection)

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS gtt_sl_id          TEXT,
    ADD COLUMN IF NOT EXISTS gtt_target_id      TEXT,
    ADD COLUMN IF NOT EXISTS sl_order_id        TEXT,
    ADD COLUMN IF NOT EXISTS target_order_id    TEXT,
    ADD COLUMN IF NOT EXISTS slippage_pct       NUMERIC(8, 4),
    ADD COLUMN IF NOT EXISTS retry_count        SMALLINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS rejection_reason   TEXT,
    ADD COLUMN IF NOT EXISTS parent_order_id    TEXT;
