-- Migration 038: Sector rotation classifications (DB truth, JSON is cache)
CREATE TABLE IF NOT EXISTS sector_rotation_log (
    id          BIGSERIAL    PRIMARY KEY,
    sector_name TEXT         NOT NULL,
    status      TEXT         NOT NULL,  -- LEADING | EMERGING | LAGGING | DECLINING
    rs_score    NUMERIC(8,4),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sector_rot_sector ON sector_rotation_log (sector_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sector_rot_status ON sector_rotation_log (status, created_at DESC);
