-- Mobile trade notifications (Telegram): sent-notification audit log +
-- idempotency ledger. NOT a job queue — the notifier is an in-process
-- asyncio.Queue + worker task inside the orchestrator (every notification
-- hook site lives in that one process; see launcher.py, which runs the API
-- server and the orchestrator as separate OS processes). This table exists so
-- (a) an entry/exit/reject notification is provably sent at most once across
-- a restart or the 30s ghost-trade reconciliation loop, and (b) delivery
-- failures are auditable after the fact.
CREATE TABLE IF NOT EXISTS notifications_sent (
    id              BIGSERIAL   PRIMARY KEY,
    kind            TEXT        NOT NULL,
    agent_trade_id  TEXT,
    symbol          TEXT,
    is_paper        BOOLEAN     NOT NULL DEFAULT TRUE,
    dedupe_key      TEXT,
    body            TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'SENT',
    error           TEXT,
    attempts        SMALLINT    NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Only once-ever events (entry/exit/reject/eod) carry a dedupe_key; modify and
-- position-update rows leave it NULL, so the partial index never collides.
CREATE UNIQUE INDEX IF NOT EXISTS uq_notifications_dedupe
    ON notifications_sent (dedupe_key) WHERE dedupe_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_notifications_created
    ON notifications_sent (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_notifications_trade
    ON notifications_sent (agent_trade_id) WHERE agent_trade_id IS NOT NULL;
