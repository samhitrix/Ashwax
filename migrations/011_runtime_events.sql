-- Migration 011: Runtime events bus (Supervisor writes, Dashboard reads)

CREATE TABLE IF NOT EXISTS runtime_events (
    id          BIGSERIAL   PRIMARY KEY,
    event_type  TEXT        NOT NULL,
    source      TEXT        NOT NULL,
    payload     JSONB,
    severity    TEXT        NOT NULL DEFAULT 'INFO',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_runtime_events_created_at ON runtime_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_events_source     ON runtime_events (source);
CREATE INDEX IF NOT EXISTS idx_runtime_events_type       ON runtime_events (event_type);
