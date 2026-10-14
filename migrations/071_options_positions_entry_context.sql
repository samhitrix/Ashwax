-- Migration 071: entry-time context on options_positions, so the post-close autopsy
-- (options_spec.md 18 / src/options/options_autopsy.py) can answer its questions after a
-- restart. All nullable: rows opened before this migration simply skip the questions that
-- need the missing input.

ALTER TABLE options_positions
    ADD COLUMN IF NOT EXISTS entry_regime       TEXT,
    ADD COLUMN IF NOT EXISTS entry_atm_iv       NUMERIC(8, 6),   -- decimal, 0.18 = 18%
    ADD COLUMN IF NOT EXISTS entry_spot         NUMERIC(12, 2),
    ADD COLUMN IF NOT EXISTS entry_short_delta  NUMERIC(6, 4),   -- |delta| of the short strike at entry
    ADD COLUMN IF NOT EXISTS was_breached       BOOLEAN NOT NULL DEFAULT FALSE;
