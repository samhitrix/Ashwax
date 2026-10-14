-- Migration 041: WebSocket health snapshot (single upserted row, DB truth).
-- The tick ingester runs in the orchestrator process; the API server is a
-- separate process, so WS health must cross the boundary via the DB.

CREATE TABLE IF NOT EXISTS ws_health (
    id          INTEGER     PRIMARY KEY,
    snapshot    JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
