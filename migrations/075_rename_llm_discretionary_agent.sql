-- Rename strategy llm_discretionary_agent -> llm_swing_agent (2026-09-21).
-- Idempotent: after the first run no row carries the old name.

-- strategy_registry_db is only a mirror of the registry JSON and has a UNIQUE
-- strategy_name; the running process may already have inserted the new-name row
-- from the renamed spec. Drop the stale old-name row if so, otherwise rename it.
DELETE FROM strategy_registry_db
 WHERE strategy_name = 'llm_discretionary_agent'
   AND EXISTS (SELECT 1 FROM strategy_registry_db WHERE strategy_name = 'llm_swing_agent');
UPDATE strategy_registry_db SET strategy_name = 'llm_swing_agent' WHERE strategy_name = 'llm_discretionary_agent';

-- History tables: keep all rows, just re-key them.
UPDATE performance_attribution SET strategy_name = 'llm_swing_agent' WHERE strategy_name = 'llm_discretionary_agent';
UPDATE trades                  SET strategy_name = 'llm_swing_agent' WHERE strategy_name = 'llm_discretionary_agent';
UPDATE paper_trades            SET strategy_name = 'llm_swing_agent' WHERE strategy_name = 'llm_discretionary_agent';
