-- Migration 055: Seller-desk historical event moves (spec §16).
-- Used to verify straddle/strangle approval: query the last 6+ occurrences of an
-- event_type; if average actual_move_pct < breakeven_required_move_pct, REJECT —
-- the event historically doesn't move enough to overcome the premium paid.

CREATE TABLE IF NOT EXISTS options_event_history (
    event_type       TEXT          NOT NULL,    -- rbi_policy | us_fed | budget | earnings_reliance | ...
    event_date       DATE          NOT NULL,
    underlying       TEXT          NOT NULL,     -- NIFTY | BANKNIFTY
    pre_event_iv     NUMERIC(8, 5),
    post_event_iv    NUMERIC(8, 5),
    actual_move_pct  NUMERIC(8, 4),              -- actual underlying move on event day
    iv_crush_pct     NUMERIC(8, 4),              -- how much IV dropped after the event
    PRIMARY KEY (event_type, event_date, underlying)
);
