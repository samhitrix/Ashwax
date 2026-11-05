-- Migration 085: persist the gap between the origination LLM's own confidence
-- and the independent G9 reviewer's confidence, for LLM-originated strategies
-- (llm_swing_agent / llm_intraday_agent) only.
--
-- Phase 1, observe-only: this data exists so a future decision about whether
-- reviewer disagreement predicts a worse outcome can be made from real samples
-- instead of a guessed threshold. It does not gate or change any trade decision.
-- NULL for rows created before this column existed, for DSL (non-LLM-originated)
-- strategies, and for any trade where the G9 reviewer never returned a verdict.

ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS llm_origination_confidence NUMERIC(4,2);
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS llm_confidence_gap NUMERIC(4,2);

ALTER TABLE trades ADD COLUMN IF NOT EXISTS llm_origination_confidence NUMERIC(4,2);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS llm_confidence_gap NUMERIC(4,2);
