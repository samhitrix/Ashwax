-- Migration 026: Alt data log (VIX, A/D ratio, GIFT Nifty, FII)

CREATE TABLE IF NOT EXISTS alt_data_log (
    id          BIGSERIAL PRIMARY KEY,
    data_type   TEXT      NOT NULL,
    value       JSONB     NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alt_data_ts        ON alt_data_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_alt_data_data_type ON alt_data_log (data_type);
