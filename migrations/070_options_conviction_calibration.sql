-- Options learning-engine PR6: per-(structure_type, is_paper) conviction
-- calibration for src/options/structure_selector.py's _p_win(). A dedicated,
-- small table rather than reusing equity's calibration_thresholds (migration
-- 040) -- that table is a single-scalar rejection FLOOR keyed by equity
-- strategy name; this is a two-scalar CLAMP-BOUND pair keyed by an options
-- structure type, a different shape and semantic. Conflating them was
-- identified as a real risk during planning, not a hypothetical one.
--
-- is_paper is part of the key (not a separate table) so a structure_type's
-- paper-trial calibration and its real-money calibration are tracked
-- independently -- options trades paper-only today, so this is currently a
-- single is_paper=true row per structure_type, but the schema is already
-- correct for the day options ever goes live, rather than requiring a
-- migration then.
CREATE TABLE IF NOT EXISTS options_conviction_calibration (
    structure_type   TEXT         NOT NULL,
    is_paper         BOOLEAN      NOT NULL,
    p_win_floor      NUMERIC(5,4) NOT NULL,
    p_win_ceiling    NUMERIC(5,4) NOT NULL,
    sample_count     INTEGER      NOT NULL DEFAULT 0,
    brier_score      NUMERIC(6,4),
    worst_gap        NUMERIC(5,4),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (structure_type, is_paper)
);
