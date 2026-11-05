-- Migration 065: Options-desk adjustment (roll) audit log (options_spec.md §10,
-- the spec's own planned "022_options_adjustment_cost_log.sql", numbered here
-- to continue this build's real migration sequence).
--
-- options_positions.adjustments_count already exists (migration 051) but was
-- never read or written -- a process restart silently reset every position's
-- adjustment count to 0, bypassing the hard MAX_ADJUSTMENTS_PER_POSITION=2
-- cap (same failure shape as the daily-loss-cap restart bug fixed earlier in
-- this project). This migration adds the audit trail; the count column
-- itself just needed real callers (see OptionsRepository.record_adjustment).

CREATE TABLE IF NOT EXISTS options_adjustment_log (
    id                  BIGSERIAL     PRIMARY KEY,
    position_id         TEXT          NOT NULL REFERENCES options_positions (position_id),
    adjusted_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    old_tradingsymbol   TEXT          NOT NULL,
    old_strike          NUMERIC(12, 2) NOT NULL,
    new_tradingsymbol   TEXT          NOT NULL,
    new_strike          NUMERIC(12, 2) NOT NULL,
    instrument_type     TEXT          NOT NULL,  -- CE | PE (the untested side that was rolled)
    adjustment_cost     NUMERIC(12, 2) NOT NULL,
    remaining_premium   NUMERIC(12, 2) NOT NULL,
    reason              TEXT          NOT NULL,
    adjustments_made    INTEGER       NOT NULL  -- position's count AFTER this adjustment
);

CREATE INDEX IF NOT EXISTS idx_options_adjustment_log_position
    ON options_adjustment_log (position_id);
