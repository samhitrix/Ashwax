-- Migration 043: Order Monitor+ telemetry (DB truth for the Advanced Order Monitor tab).
-- The OrderMonitor cycle runs in the orchestrator process; the API server is a
-- separate process, so monitor stats + breach events cross the boundary via the DB.

-- Live monitor stats — single upserted row (id = 1), mirrors ws_health pattern.
CREATE TABLE IF NOT EXISTS order_monitor_stats (
    id                  INTEGER     PRIMARY KEY,
    check_interval_ms   INTEGER     NOT NULL DEFAULT 0,
    checks_per_minute   INTEGER     NOT NULL DEFAULT 0,
    avg_latency_ms      INTEGER     NOT NULL DEFAULT 0,
    orders_monitored    INTEGER     NOT NULL DEFAULT 0,
    breaches_detected   INTEGER     NOT NULL DEFAULT 0,
    auto_actions        INTEGER     NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One row per detected breach / proximity event, append-only.
CREATE TABLE IF NOT EXISTS order_monitor_breaches (
    id              BIGSERIAL   PRIMARY KEY,
    agent_trade_id  TEXT,
    symbol          TEXT        NOT NULL,
    breach_type     TEXT        NOT NULL,  -- TARGET_APPROACHING | SL_APPROACHING | TARGET_BREACH | SL_BREACH | GTT_MISSING
    ltp             NUMERIC(12, 2),
    expected_price  NUMERIC(12, 2),
    gap_pct         NUMERIC(8, 4),
    order_type      TEXT,                  -- LIMIT | SL | GTT
    auto_action     TEXT        NOT NULL DEFAULT 'NONE',
    action_result   TEXT,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_monitor_breaches_detected ON order_monitor_breaches (detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_monitor_breaches_symbol   ON order_monitor_breaches (symbol);
