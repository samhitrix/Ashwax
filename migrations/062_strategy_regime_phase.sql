-- Regime Pullback Swing v1.0 (spec §8.2): per strategy+symbol tracking of the
-- current continuous in-regime phase, so CRM can block re-entry after
-- RegimeFilter.max_attempts_per_phase consecutive stop-outs within one phase.
-- attempts are DERIVED at read time (count of losing closed trades since
-- phase_started_at) rather than incremented here, so they can never drift
-- from the trades table (DB = truth).
CREATE TABLE IF NOT EXISTS strategy_regime_phase (
    strategy_name   TEXT        NOT NULL,
    symbol          TEXT        NOT NULL,
    regime_ok       BOOLEAN     NOT NULL DEFAULT FALSE,
    phase_started_at TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_name, symbol)
);
