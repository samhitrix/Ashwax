-- Migration 047: persist the G9 advisory LLM verdict on each trade row.
-- The verdict (confidence / flag / reason) was previously only in logs/llm.log,
-- so it was invisible on the dashboard. Surfaced in the Paper Trades tab.
-- NULL for rows created before this column existed, or when no provider evaluated.

ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS llm_confidence NUMERIC(4,2);
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS llm_flag TEXT;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS llm_reason TEXT;

ALTER TABLE trades ADD COLUMN IF NOT EXISTS llm_confidence NUMERIC(4,2);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS llm_flag TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS llm_reason TEXT;
