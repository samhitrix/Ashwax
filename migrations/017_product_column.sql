-- Migration 017: Add product column to trades (MIS / CNC / NRML)

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS product TEXT NOT NULL DEFAULT 'MIS'
        CHECK (product IN ('MIS', 'CNC', 'NRML'));
