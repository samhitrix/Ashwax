-- Migration 061: paper/live discriminator for options tables (Phase 2 — per-segment
-- concurrent paper/live). Neither options_positions (seller-first) nor
-- options_structures (buyer-first) has ever distinguished paper from live rows —
-- every historical row is ambiguous. DEFAULT TRUE is the fail-safe direction:
-- mislabeling a live row as paper only affects reporting, never execution.

ALTER TABLE options_positions ADD COLUMN IF NOT EXISTS is_paper BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE options_structures ADD COLUMN IF NOT EXISTS is_paper BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_options_positions_is_paper ON options_positions (is_paper);
CREATE INDEX IF NOT EXISTS idx_options_structures_is_paper ON options_structures (is_paper);
