-- Migration 091: per-broker reconciliation detail (multi-broker routing, phase 2, task 9).
--
-- The orchestrator now reconciles every live broker separately. reconciliation_log keeps its
-- existing columns for the PRIMARY live broker (the dashboard reads them unchanged); this nullable
-- column holds {broker: {db_positions, matched, auto_closed, gaps}} for every live broker.
-- Purely additive and idempotent; written by a separate best-effort UPDATE, so a database without
-- it only loses the per-broker detail.

ALTER TABLE reconciliation_log ADD COLUMN IF NOT EXISTS by_broker JSONB;
