-- Migration 073: repair options_conviction_calibration on databases where an earlier draft of
-- migration 070 had already created the table (single-column PK on structure_type, no
-- is_paper). 070's CREATE TABLE IF NOT EXISTS was then a no-op, so every load/save that
-- filters or conflicts on (structure_type, is_paper) failed with
-- 'column "is_paper" does not exist'. Idempotent; safe on databases created from the
-- current 070 (nothing to change). Existing rows (there were none) become is_paper = TRUE,
-- matching "options trades paper-only".

ALTER TABLE options_conviction_calibration
    ADD COLUMN IF NOT EXISTS is_paper BOOLEAN NOT NULL DEFAULT TRUE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'options_conviction_calibration'::regclass
          AND contype = 'p'
          AND pg_get_constraintdef(oid) = 'PRIMARY KEY (structure_type)'
    ) THEN
        ALTER TABLE options_conviction_calibration
            DROP CONSTRAINT options_conviction_calibration_pkey;
        ALTER TABLE options_conviction_calibration
            ADD PRIMARY KEY (structure_type, is_paper);
    END IF;
END $$;
