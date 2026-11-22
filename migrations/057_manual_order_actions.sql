-- Migration 057: Manual order actions queue.
-- The dashboard's "Market Exit" button lives in the API process, which has no
-- reference to the live OrderMonitor (that object only exists inside the
-- orchestrator process). This table is the command channel across that
-- boundary, mirroring how order_monitor_stats/order_monitor_breaches already
-- cross it in the other direction (see migration 043).

CREATE TABLE IF NOT EXISTS manual_order_actions (
    id              BIGSERIAL   PRIMARY KEY,
    agent_trade_id  TEXT        NOT NULL,
    action          TEXT        NOT NULL,  -- MARKET_EXIT (more actions later)
    status          TEXT        NOT NULL DEFAULT 'PENDING',  -- PENDING | DONE | FAILED
    result          TEXT,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_manual_order_actions_pending
    ON manual_order_actions (status, requested_at) WHERE status = 'PENDING';
