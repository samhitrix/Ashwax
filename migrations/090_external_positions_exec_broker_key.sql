-- Migration 090: one OPEN external_positions row per (symbol, product, BROKER) -- multi-broker
-- routing, phase 2 (task 9: per-broker reconciliation).
--
-- 074 allowed one OPEN row per (symbol, product). With one reconciler per live broker the same
-- symbol/product can be an orphan on two brokers at once; each must be tracked (and acknowledged)
-- on its own, so the key gains the broker. 089 added the exec_broker column (NULL on every row
-- written before routing existed); NULL means 'kite' everywhere in phase 2, so the key uses
-- COALESCE(exec_broker, 'kite') and a NULL row collides with a 'kite' row, never with another
-- broker's.
--
-- Safe and idempotent: the new index is strictly weaker than 074's (any table with unique
-- (symbol, product) among OPEN rows is also unique on (symbol, product, broker)), so creating it
-- cannot fail on existing data. It is created BEFORE the old index is dropped, so there is no
-- window without a uniqueness guard; both statements are no-ops on a re-run. No data changes.

CREATE UNIQUE INDEX IF NOT EXISTS uq_external_positions_open_broker
    ON external_positions (symbol, product, (COALESCE(exec_broker, 'kite')))
    WHERE status = 'OPEN';

DROP INDEX IF EXISTS uq_external_positions_open;
