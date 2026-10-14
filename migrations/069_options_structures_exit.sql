-- Options learning-engine PR5: exit-side tracking for buyer-first
-- (src/options/structure_selector.py) options_structures rows. Confirmed gap:
-- this table had an insert path only -- no column or code path ever recorded
-- whether a placed structure won or lost, so nothing could calibrate against
-- real outcomes. Additive, nullable columns only -- zero risk to existing rows
-- or readers. `target`/`entry_conviction` were already computed on
-- StructureChoice/Opportunity at entry time but silently dropped by
-- insert_structure() -- added here alongside the exit columns since both are
-- "make buyer-first structures observable", one cohesive prerequisite for
-- PR6's conviction calibration.
ALTER TABLE options_structures
    ADD COLUMN IF NOT EXISTS closed_at        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS realized_pnl     NUMERIC(12, 2),
    ADD COLUMN IF NOT EXISTS exit_reason      TEXT,
    ADD COLUMN IF NOT EXISTS target           NUMERIC(12, 2),
    ADD COLUMN IF NOT EXISTS entry_conviction NUMERIC(5, 4);
