-- Migration 053: Seller-desk multi-leg execution log (spec §9).
-- actual_fill_prices is THE TRUTH (not intended). Records partial fills, naked
-- exposure duration, and any emergency action — every naked-exposure firing is a
-- bug to investigate (protection-first submission should make it impossible).

CREATE TABLE IF NOT EXISTS options_execution_log (
    id                              BIGSERIAL    PRIMARY KEY,
    position_id                     TEXT         NOT NULL,
    basket_order_id                 TEXT,
    leg_count                       INTEGER      NOT NULL,
    legs_filled                     INTEGER      NOT NULL,
    legs_rejected                   INTEGER      NOT NULL,
    partial_fill                    BOOLEAN      NOT NULL DEFAULT FALSE,
    naked_exposure_detected         BOOLEAN      NOT NULL DEFAULT FALSE,
    naked_exposure_duration_seconds NUMERIC(8, 2),
    emergency_action_taken          TEXT,
    intended_prices                 JSONB,       -- per leg
    actual_fill_prices              JSONB,       -- per leg (the truth)
    slippage_per_leg                JSONB,
    total_margin_used               NUMERIC(14, 2),
    impact_cost                     NUMERIC(12, 2),
    timestamp                       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_options_exec_log_position ON options_execution_log (position_id);
