-- Migration 022: RS (Relative Strength) rankings

CREATE TABLE IF NOT EXISTS rs_rankings (
    id              BIGSERIAL   PRIMARY KEY,
    symbol          TEXT        NOT NULL,
    rs_score        NUMERIC(8, 4) NOT NULL,
    percentile_rank SMALLINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rs_rankings_symbol     ON rs_rankings (symbol);
CREATE INDEX IF NOT EXISTS idx_rs_rankings_created_at ON rs_rankings (created_at DESC);
