-- Migration 059: trade_insights.trade_id can reference either paper_trades(id) or
-- trades(id) (paper vs live) — Postgres has no clean "FK to either table" construct,
-- so drop the strict FK that only ever allowed live trades(id). In paper mode
-- (trades empty), every insight save was silently violating this constraint.
-- Validity of trade_id is enforced at the application layer (both source tables
-- share a bigint PK sequence-space, never reused across the two).

ALTER TABLE trade_insights DROP CONSTRAINT IF EXISTS trade_insights_trade_id_fkey;
