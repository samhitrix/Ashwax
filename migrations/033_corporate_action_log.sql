-- Migration 033: Corporate action log (split/bonus/dividend SL adjustments)

CREATE TABLE IF NOT EXISTS corporate_action_log (
    id                    BIGSERIAL PRIMARY KEY,
    symbol                TEXT      NOT NULL,
    action_type           TEXT      NOT NULL
                              CHECK (action_type IN ('SPLIT', 'BONUS', 'DIVIDEND')),
    ratio_or_amount       NUMERIC(12, 4),
    action_date           DATE      NOT NULL,
    trade_id              BIGINT    REFERENCES trades (id) ON DELETE SET NULL,
    adjustment_applied    JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_corp_action_symbol ON corporate_action_log (symbol);
CREATE INDEX IF NOT EXISTS idx_corp_action_date   ON corporate_action_log (action_date DESC);
