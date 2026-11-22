-- Migration 092: persisted per-broker drawdown baseline (system.per_broker_equity).
--
-- With per-broker equity on, each live broker's 25% equity floor and drawdown staircase
-- measure from a baseline equity. It was the first equity fetched by THIS process, so a
-- restart silently reset a broker's drawdown (a broker down 9% came back at full size).
-- The first successful fetch now writes the baseline here ONCE (INSERT ... ON CONFLICT DO
-- NOTHING); every later process reads it back. Only an operator resets it: delete or
-- update the row, then restart. Purely additive and idempotent.

CREATE TABLE IF NOT EXISTS broker_equity_baseline (
    stack_key       TEXT PRIMARY KEY,            -- "live:<broker>", e.g. "live:kite"
    baseline_equity NUMERIC(18, 2) NOT NULL CHECK (baseline_equity > 0),
    set_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
