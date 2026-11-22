-- Migration 052: Seller-desk event calendar & dynamic blackouts (spec §16).
-- Layer 1 = scheduled events (RBI/Fed/Budget/rebalance/earnings), pre-loaded.
-- Layer 2 = dynamic MACRO blackouts from the news engine (auto-expiring TTL).
-- Selling is blocked within the blackout window; underlying NULL = applies to all.

CREATE TABLE IF NOT EXISTS options_event_calendar (
    id             BIGSERIAL    PRIMARY KEY,
    event_type     TEXT         NOT NULL,    -- rbi_policy | us_fed | budget | rebalance | earnings_* | macro_news
    event_date     DATE,                     -- scheduled events; NULL for pure dynamic blackouts
    underlying     TEXT,                     -- NULL = all index options
    layer          TEXT         NOT NULL,    -- scheduled | dynamic
    blackout_from  TIMESTAMPTZ,
    blackout_until TIMESTAMPTZ,              -- dynamic blackouts carry an explicit expiry
    source         TEXT,                     -- manual | nse_corp_actions | news_engine
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_options_event_calendar_date ON options_event_calendar (event_date);
CREATE INDEX IF NOT EXISTS idx_options_event_calendar_until ON options_event_calendar (blackout_until);
