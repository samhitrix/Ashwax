-- Migration 023: Earnings calendar (NSE fetch, 5-day block window)

CREATE TABLE IF NOT EXISTS earnings_calendar (
    id           BIGSERIAL PRIMARY KEY,
    symbol       TEXT      NOT NULL,
    report_date  DATE      NOT NULL,
    period       TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, report_date)
);

CREATE INDEX IF NOT EXISTS idx_earnings_symbol      ON earnings_calendar (symbol);
CREATE INDEX IF NOT EXISTS idx_earnings_report_date ON earnings_calendar (report_date);
