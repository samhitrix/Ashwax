-- Migration 056: Seller-desk options autopsy findings (options_spec.md §18).
-- Not a FK to `trades` (equity table) — options structures live in
-- options_structures/options_positions, a separate ID space.

CREATE TABLE IF NOT EXISTS options_learning_log (
    id                  BIGSERIAL    PRIMARY KEY,
    position_id         TEXT         NOT NULL,
    question            TEXT         NOT NULL,   -- e.g. 'correct_strategy_for_regime'
    finding             TEXT         NOT NULL,
    confidence          NUMERIC(4,2),
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_options_learning_position ON options_learning_log (position_id);
CREATE INDEX IF NOT EXISTS idx_options_learning_question ON options_learning_log (question);
