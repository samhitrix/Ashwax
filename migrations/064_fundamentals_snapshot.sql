-- Per-symbol-per-date fundamentals snapshot (src/market_data/fundamentals_fetcher.py,
-- yfinance MVP source, config/global.json fundamentals.enabled). Log-only: never
-- read by any CRM gate or the G9 prompt until scripts/backtest_fundamentals_filter.py
-- validates a real, out-of-sample edge (see that script's own header).
CREATE TABLE IF NOT EXISTS fundamentals_snapshot (
    id                BIGSERIAL   PRIMARY KEY,
    symbol            TEXT        NOT NULL,
    snapshot_date     DATE        NOT NULL,
    trailing_pe       NUMERIC(12,2),
    forward_pe        NUMERIC(12,2),
    trailing_eps      NUMERIC(14,4),
    forward_eps       NUMERIC(14,4),
    earnings_growth   NUMERIC(10,4),
    revenue_growth    NUMERIC(10,4),
    return_on_equity  NUMERIC(10,4),
    debt_to_equity    NUMERIC(12,2),
    market_cap        BIGINT,
    dividend_yield    NUMERIC(8,4),
    yahoo_sector      TEXT,
    source            TEXT        NOT NULL DEFAULT 'yfinance',
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_fundamentals_snapshot_symbol ON fundamentals_snapshot (symbol);
CREATE INDEX IF NOT EXISTS idx_fundamentals_snapshot_date   ON fundamentals_snapshot (snapshot_date DESC);
