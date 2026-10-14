-- Migration 012: Engine heartbeats (one row per engine, upserted each cycle)

CREATE TABLE IF NOT EXISTS engine_heartbeats (
    engine_name TEXT        PRIMARY KEY,
    pid         INTEGER,
    last_beat   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status      TEXT        NOT NULL DEFAULT 'ALIVE',
    metadata    JSONB
);
