-- Migration 020: News events

CREATE TABLE IF NOT EXISTS news_events (
    id                      BIGSERIAL   PRIMARY KEY,
    symbol                  TEXT,
    headline                TEXT        NOT NULL,
    source                  TEXT        NOT NULL,
    event_type              TEXT        NOT NULL,
    sentiment               TEXT        NOT NULL
                                CHECK (sentiment IN ('POSITIVE', 'NEGATIVE', 'NEUTRAL')),
    magnitude               NUMERIC(4, 2),
    trading_action          TEXT,
    avoid_duration_minutes  INTEGER,
    confidence              NUMERIC(4, 2),
    reasoning               TEXT,
    affected_symbols        JSONB,
    ts                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_news_events_symbol ON news_events (symbol);
CREATE INDEX IF NOT EXISTS idx_news_events_ts     ON news_events (ts DESC);
