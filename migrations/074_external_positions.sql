-- Migration 074: positions the broker holds that this agent did not open (manual trades,
-- legacy holdings, cash-parking ETFs such as LIQUIDBEES). The reconciler used to re-report
-- them as ORPHANED_BROKER_POSITION on every 30s cycle forever. They are now recorded ONCE
-- here (first_seen / last_seen / times_seen), closed automatically when the broker position
-- disappears, and can be ACKNOWLEDGED with a classification + note, after which the
-- reconciler stops reporting them as gaps. Purely additive; no other table changes.

CREATE TABLE IF NOT EXISTS external_positions (
    id              BIGSERIAL    PRIMARY KEY,
    symbol          TEXT         NOT NULL,
    product         TEXT         NOT NULL,             -- canonical: DELIVERY | INTRADAY | MARGIN
    quantity        INTEGER      NOT NULL,
    avg_price       NUMERIC(14, 2),
    status          TEXT         NOT NULL DEFAULT 'OPEN',   -- OPEN | CLOSED
    classification  TEXT         NOT NULL DEFAULT 'UNCLASSIFIED',
                                 -- UNCLASSIFIED | MANUAL | CASH_PARKING | LEGACY | OTHER
    acknowledged    BOOLEAN      NOT NULL DEFAULT FALSE,
    note            TEXT,
    first_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    times_seen      INTEGER      NOT NULL DEFAULT 1
);

-- At most one OPEN row per (symbol, product); closed history is kept.
CREATE UNIQUE INDEX IF NOT EXISTS uq_external_positions_open
    ON external_positions (symbol, product) WHERE status = 'OPEN';
