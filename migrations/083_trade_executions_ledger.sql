-- Migration 083: append-only execution ledger for EVERY instrument (equity intraday, equity swing,
-- options; paper and live).
--
-- Why: trades / paper_trades / options_positions are ONE MUTABLE ROW per trade. A partial exit
-- shrank `quantity` in place, so the original size, the partial's price/time and the SL history
-- were destroyed (2026-09-28: LAURUSLABS showed 13 shares next to P&L for 26; 13 older partial
-- trades had P&L booked but no sold quantity anywhere). options_execution_log and
-- options_adjustment_log exist but were never written.
--
-- How: DB triggers, not application code. Every write path (repositories, the paper engine's raw
-- SQL, scripts, manual edits) changes these three tables, so the database itself records every
-- entry fill, partial exit, final exit, stop/target move, adjustment and manual edit. Nothing can
-- bypass it. A ledger failure must NEVER block a trade write: each trigger swallows its own error
-- and records a LEDGER_ERROR row (the app reports those).
--
-- Rows are only ever INSERTed. `source` = DB_TRIGGER (live capture) or BACKFILL (reconstructed
-- from the row as it stood when this migration ran; times/quantities the old schema never kept are
-- flagged qty_known=false / detail.time_estimated, never invented).

ALTER TABLE trades        ADD COLUMN IF NOT EXISTS original_quantity  INTEGER;
ALTER TABLE paper_trades  ADD COLUMN IF NOT EXISTS original_quantity  INTEGER;
ALTER TABLE trades        ADD COLUMN IF NOT EXISTS exit_reason        TEXT;
ALTER TABLE trades        ADD COLUMN IF NOT EXISTS last_partial_price NUMERIC;
ALTER TABLE paper_trades  ADD COLUMN IF NOT EXISTS last_partial_price NUMERIC;

CREATE TABLE IF NOT EXISTS trade_executions (
    id           BIGSERIAL PRIMARY KEY,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    instrument   TEXT        NOT NULL,            -- EQUITY | OPTIONS
    segment      TEXT        NOT NULL,            -- INTRADAY | SWING | OPTIONS
    is_paper     BOOLEAN     NOT NULL,
    trade_id     TEXT        NOT NULL,            -- agent_trade_id / options position_id
    symbol       TEXT,
    strategy     TEXT,
    side         TEXT,
    event        TEXT        NOT NULL,            -- see trade_executions_event_doc below
    quantity     NUMERIC,                         -- shares moved by THIS event (NULL = n/a)
    qty_known    BOOLEAN     NOT NULL DEFAULT TRUE,
    price        NUMERIC,
    pnl          NUMERIC,                         -- realised P&L of THIS leg only
    reason       TEXT,
    old_value    TEXT,
    new_value    TEXT,
    source       TEXT        NOT NULL DEFAULT 'DB_TRIGGER',
    detail       JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_trade_executions_trade ON trade_executions (trade_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_trade_executions_time  ON trade_executions (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_executions_event ON trade_executions (event, occurred_at DESC);
COMMENT ON TABLE trade_executions IS
  'Append-only. Events: ORDER_CREATED, STATE_CHANGE, ENTRY_FILL, PARTIAL_EXIT, FINAL_EXIT, SL_MOVE, '
  'TARGET_MOVE, FILL_PRICE_CORRECTED, QTY_CORRECTED, CLOSED_ROW_EDITED, ADJUSTMENT, BREACHED, DELETED, '
  'LEDGER_ERROR. Written by triggers on trades / paper_trades / options_positions.';

-- original_quantity: size when the trade was opened, never reduced by partials.
CREATE OR REPLACE FUNCTION trade_original_qty() RETURNS trigger AS $$
BEGIN
    IF NEW.original_quantity IS NULL THEN
        NEW.original_quantity := COALESCE(NEW.quantity, 0) + COALESCE(NEW.partial_qty_booked, 0);
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_trades_original_qty ON trades;
CREATE TRIGGER trg_trades_original_qty BEFORE INSERT OR UPDATE ON trades
    FOR EACH ROW EXECUTE FUNCTION trade_original_qty();
DROP TRIGGER IF EXISTS trg_paper_trades_original_qty ON paper_trades;
CREATE TRIGGER trg_paper_trades_original_qty BEFORE INSERT OR UPDATE ON paper_trades
    FOR EACH ROW EXECUTE FUNCTION trade_original_qty();

-- Equity ledger (trades = live, paper_trades = paper; intraday vs swing from `product`).
CREATE OR REPLACE FUNCTION trade_executions_equity_ledger() RETURNS trigger AS $$
DECLARE
    paper boolean := (TG_TABLE_NAME = 'paper_trades');
    r     record;
    seg   text;
BEGIN
    IF TG_OP = 'DELETE' THEN r := OLD; ELSE r := NEW; END IF;
    seg := CASE WHEN upper(coalesce(r.product, '')) IN ('MIS', 'INTRADAY') THEN 'INTRADAY' ELSE 'SWING' END;
    BEGIN
        IF TG_OP = 'INSERT' THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                          event, quantity, price, new_value)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                    'ORDER_CREATED', r.quantity, r.intended_price, r.order_state);
            IF r.order_state = 'FILLED' THEN
                INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                              event, quantity, price)
                VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                        'ENTRY_FILL', r.quantity, r.fill_price);
            END IF;
            RETURN NULL;
        ELSIF TG_OP = 'DELETE' THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                          event, quantity, price, old_value, detail)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                    'DELETED', r.quantity, r.fill_price, r.order_state, to_jsonb(OLD));
            RETURN NULL;
        END IF;

        -- UPDATE ---------------------------------------------------------------------------
        IF OLD.order_state IS DISTINCT FROM NEW.order_state THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                          event, old_value, new_value, reason)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                    'STATE_CHANGE', OLD.order_state, NEW.order_state, NEW.rejection_reason);
            IF NEW.order_state = 'FILLED' AND OLD.order_state NOT IN ('FILLED', 'CLOSED') THEN
                INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                              event, quantity, price)
                VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                        'ENTRY_FILL', NEW.quantity, NEW.fill_price);
            END IF;
            IF NEW.order_state = 'CLOSED' THEN
                INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                              event, quantity, price, pnl, reason, detail)
                VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                        'FINAL_EXIT', NEW.quantity, NEW.exit_price,
                        NEW.pnl_realized - COALESCE(NEW.partial_pnl_booked, 0), NEW.exit_reason,
                        jsonb_build_object('trade_total_pnl', NEW.pnl_realized,
                                           'partial_pnl_booked', NEW.partial_pnl_booked,
                                           'original_quantity', NEW.original_quantity));
            END IF;
        END IF;

        -- A partial exit shrinks quantity while the trade stays open.
        IF OLD.order_state = 'FILLED' AND NEW.order_state = 'FILLED' THEN
            IF NEW.quantity < OLD.quantity THEN
                INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                              event, quantity, price, pnl, detail)
                VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                        'PARTIAL_EXIT', OLD.quantity - NEW.quantity, NEW.last_partial_price,
                        NEW.partial_pnl_booked - COALESCE(OLD.partial_pnl_booked, 0),
                        jsonb_build_object('remaining_quantity', NEW.quantity,
                                           'original_quantity', NEW.original_quantity,
                                           'price_recorded', NEW.last_partial_price IS NOT NULL));
            ELSIF NEW.quantity > OLD.quantity THEN
                INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                              event, old_value, new_value)
                VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                        'QTY_CORRECTED', OLD.quantity::text, NEW.quantity::text);
            END IF;
            IF OLD.fill_price IS NOT NULL AND NEW.fill_price IS DISTINCT FROM OLD.fill_price THEN
                INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                              event, old_value, new_value)
                VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                        'FILL_PRICE_CORRECTED', OLD.fill_price::text, NEW.fill_price::text);
            END IF;
        END IF;

        IF NEW.current_sl IS DISTINCT FROM OLD.current_sl THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                          event, old_value, new_value)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                    'SL_MOVE', OLD.current_sl::text, NEW.current_sl::text);
        END IF;
        IF NEW.current_target IS DISTINCT FROM OLD.current_target THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                          event, old_value, new_value)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                    'TARGET_MOVE', OLD.current_target::text, NEW.current_target::text);
        END IF;

        -- Somebody edited a row that was already closed (backfill script, manual SQL, a late writer).
        IF OLD.order_state = 'CLOSED' AND NEW.order_state = 'CLOSED'
           AND (NEW.pnl_realized IS DISTINCT FROM OLD.pnl_realized
                OR NEW.exit_price IS DISTINCT FROM OLD.exit_price
                OR NEW.quantity IS DISTINCT FROM OLD.quantity
                OR NEW.partial_qty_booked IS DISTINCT FROM OLD.partial_qty_booked
                OR NEW.partial_pnl_booked IS DISTINCT FROM OLD.partial_pnl_booked) THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                          event, detail)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                    'CLOSED_ROW_EDITED',
                    jsonb_build_object('pnl_realized', jsonb_build_array(OLD.pnl_realized, NEW.pnl_realized),
                                       'exit_price', jsonb_build_array(OLD.exit_price, NEW.exit_price),
                                       'quantity', jsonb_build_array(OLD.quantity, NEW.quantity),
                                       'partial_qty_booked',
                                       jsonb_build_array(OLD.partial_qty_booked, NEW.partial_qty_booked),
                                       'partial_pnl_booked',
                                       jsonb_build_array(OLD.partial_pnl_booked, NEW.partial_pnl_booked)));
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'trade_executions ledger write failed for %: %', r.agent_trade_id, SQLERRM;
        BEGIN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, event, detail)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, 'LEDGER_ERROR',
                    jsonb_build_object('error', SQLERRM, 'op', TG_OP, 'table', TG_TABLE_NAME));
        EXCEPTION WHEN OTHERS THEN NULL;
        END;
    END;
    RETURN NULL;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_trades_ledger ON trades;
CREATE TRIGGER trg_trades_ledger AFTER INSERT OR UPDATE OR DELETE ON trades
    FOR EACH ROW EXECUTE FUNCTION trade_executions_equity_ledger();
DROP TRIGGER IF EXISTS trg_paper_trades_ledger ON paper_trades;
CREATE TRIGGER trg_paper_trades_ledger AFTER INSERT OR UPDATE OR DELETE ON paper_trades
    FOR EACH ROW EXECUTE FUNCTION trade_executions_equity_ledger();

-- Options ledger.
CREATE OR REPLACE FUNCTION trade_executions_options_ledger() RETURNS trigger AS $$
DECLARE
    r record;
BEGIN
    IF TG_OP = 'DELETE' THEN r := OLD; ELSE r := NEW; END IF;
    BEGIN
        IF TG_OP = 'INSERT' THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy,
                                          event, new_value, detail)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type,
                    'ENTRY_FILL', r.status,
                    jsonb_build_object('legs', r.legs, 'expiry', r.expiry, 'net_credit', r.net_credit,
                                       'max_loss', r.max_loss, 'max_profit', r.max_profit,
                                       'entry_margin', r.entry_margin, 'lot_size', r.lot_size_at_entry,
                                       'entry_spot', r.entry_spot));
            RETURN NULL;
        ELSIF TG_OP = 'DELETE' THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy,
                                          event, old_value, detail)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type,
                    'DELETED', r.status, to_jsonb(OLD));
            RETURN NULL;
        END IF;

        IF NEW.legs IS DISTINCT FROM OLD.legs THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy,
                                          event, detail)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type,
                    'ADJUSTMENT',
                    jsonb_build_object('old_legs', OLD.legs, 'new_legs', NEW.legs,
                                       'adjustments_count', NEW.adjustments_count));
        END IF;
        IF NEW.was_breached IS TRUE AND OLD.was_breached IS DISTINCT FROM TRUE THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, event)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type, 'BREACHED');
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy,
                                          event, old_value, new_value, reason)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type,
                    'STATE_CHANGE', OLD.status, NEW.status, NEW.exit_reason);
        END IF;
        IF OLD.closed_at IS NULL AND NEW.closed_at IS NOT NULL THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy,
                                          event, occurred_at, pnl, reason, detail)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type,
                    'FINAL_EXIT', NEW.closed_at, NEW.realized_pnl, NEW.exit_reason,
                    jsonb_build_object('legs', NEW.legs, 'adjustments_count', NEW.adjustments_count));
        ELSIF OLD.closed_at IS NOT NULL
              AND (NEW.realized_pnl IS DISTINCT FROM OLD.realized_pnl
                   OR NEW.closed_at IS DISTINCT FROM OLD.closed_at) THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy,
                                          event, detail)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type,
                    'CLOSED_ROW_EDITED',
                    jsonb_build_object('realized_pnl', jsonb_build_array(OLD.realized_pnl, NEW.realized_pnl),
                                       'closed_at', jsonb_build_array(OLD.closed_at, NEW.closed_at)));
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'trade_executions options ledger write failed for %: %', r.position_id, SQLERRM;
        BEGIN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, event, detail)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, 'LEDGER_ERROR',
                    jsonb_build_object('error', SQLERRM, 'op', TG_OP));
        EXCEPTION WHEN OTHERS THEN NULL;
        END;
    END;
    RETURN NULL;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_options_positions_ledger ON options_positions;
CREATE TRIGGER trg_options_positions_ledger AFTER INSERT OR UPDATE OR DELETE ON options_positions
    FOR EACH ROW EXECUTE FUNCTION trade_executions_options_ledger();

-- ── Backfill: reconstruct what the existing rows still know (idempotent). original_quantity is set
-- FIRST; the ledger triggers ignore that column so this writes no noise.
UPDATE trades       SET original_quantity = quantity + COALESCE(partial_qty_booked, 0) WHERE original_quantity IS NULL;
UPDATE paper_trades SET original_quantity = quantity + COALESCE(partial_qty_booked, 0) WHERE original_quantity IS NULL;

-- trades: entry fill
INSERT INTO trade_executions (occurred_at, instrument, segment, is_paper, trade_id, symbol, strategy, side,
                              event, quantity, price, source)
SELECT COALESCE(t.fill_timestamp, t.entry_timestamp, t.created_at), 'EQUITY', CASE WHEN upper(coalesce(t.product, '')) IN ('MIS', 'INTRADAY') THEN 'INTRADAY' ELSE 'SWING' END, false,
       t.agent_trade_id, t.symbol, t.strategy_name, t.direction, 'ENTRY_FILL',
       t.original_quantity, t.fill_price, 'BACKFILL'
  FROM trades t
 WHERE t.fill_price IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM trade_executions e WHERE e.trade_id = t.agent_trade_id);

-- trades: partial exit -- only the running totals survive; quantity is unknown unless partial_qty_booked
-- was recorded, price and time were never kept (occurred_at is the best estimate).
INSERT INTO trade_executions (occurred_at, instrument, segment, is_paper, trade_id, symbol, strategy, side,
                              event, quantity, qty_known, pnl, source, detail)
SELECT COALESCE(t.exit_timestamp, t.updated_at), 'EQUITY', CASE WHEN upper(coalesce(t.product, '')) IN ('MIS', 'INTRADAY') THEN 'INTRADAY' ELSE 'SWING' END, false,
       t.agent_trade_id, t.symbol, t.strategy_name, t.direction, 'PARTIAL_EXIT',
       NULLIF(t.partial_qty_booked, 0), COALESCE(t.partial_qty_booked, 0) > 0, t.partial_pnl_booked,
       'BACKFILL', jsonb_build_object('time_estimated', true, 'price_recorded', false)
  FROM trades t
 WHERE COALESCE(t.partial_pnl_booked, 0) <> 0
   AND EXISTS (SELECT 1 FROM trade_executions e WHERE e.trade_id = t.agent_trade_id AND e.source = 'BACKFILL'
                AND e.event = 'ENTRY_FILL')
   AND NOT EXISTS (SELECT 1 FROM trade_executions e WHERE e.trade_id = t.agent_trade_id
                    AND e.event = 'PARTIAL_EXIT');

-- trades: final exit. pnl left NULL: older rows hold last-leg-only P&L, newer ones the whole trade.
INSERT INTO trade_executions (occurred_at, instrument, segment, is_paper, trade_id, symbol, strategy, side,
                              event, quantity, price, reason, source, detail)
SELECT t.exit_timestamp, 'EQUITY', CASE WHEN upper(coalesce(t.product, '')) IN ('MIS', 'INTRADAY') THEN 'INTRADAY' ELSE 'SWING' END, false,
       t.agent_trade_id, t.symbol, t.strategy_name, t.direction, 'FINAL_EXIT',
       t.quantity, t.exit_price, t.exit_reason, 'BACKFILL',
       jsonb_build_object('row_pnl_realized', t.pnl_realized, 'partial_pnl_booked', t.partial_pnl_booked,
                          'original_quantity', t.original_quantity)
  FROM trades t
 WHERE t.order_state = 'CLOSED' AND t.exit_timestamp IS NOT NULL
   AND EXISTS (SELECT 1 FROM trade_executions e WHERE e.trade_id = t.agent_trade_id AND e.source = 'BACKFILL'
                AND e.event = 'ENTRY_FILL')
   AND NOT EXISTS (SELECT 1 FROM trade_executions e WHERE e.trade_id = t.agent_trade_id
                    AND e.event = 'FINAL_EXIT');

-- paper_trades: entry fill
INSERT INTO trade_executions (occurred_at, instrument, segment, is_paper, trade_id, symbol, strategy, side,
                              event, quantity, price, source)
SELECT COALESCE(t.fill_timestamp, t.entry_timestamp, t.created_at), 'EQUITY', CASE WHEN upper(coalesce(t.product, '')) IN ('MIS', 'INTRADAY') THEN 'INTRADAY' ELSE 'SWING' END, true,
       t.agent_trade_id, t.symbol, t.strategy_name, t.direction, 'ENTRY_FILL',
       t.original_quantity, t.fill_price, 'BACKFILL'
  FROM paper_trades t
 WHERE t.fill_price IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM trade_executions e WHERE e.trade_id = t.agent_trade_id);

-- paper_trades: partial exit -- only the running totals survive; quantity is unknown unless partial_qty_booked
-- was recorded, price and time were never kept (occurred_at is the best estimate).
INSERT INTO trade_executions (occurred_at, instrument, segment, is_paper, trade_id, symbol, strategy, side,
                              event, quantity, qty_known, pnl, source, detail)
SELECT COALESCE(t.exit_timestamp, t.updated_at), 'EQUITY', CASE WHEN upper(coalesce(t.product, '')) IN ('MIS', 'INTRADAY') THEN 'INTRADAY' ELSE 'SWING' END, true,
       t.agent_trade_id, t.symbol, t.strategy_name, t.direction, 'PARTIAL_EXIT',
       NULLIF(t.partial_qty_booked, 0), COALESCE(t.partial_qty_booked, 0) > 0, t.partial_pnl_booked,
       'BACKFILL', jsonb_build_object('time_estimated', true, 'price_recorded', false)
  FROM paper_trades t
 WHERE COALESCE(t.partial_pnl_booked, 0) <> 0
   AND EXISTS (SELECT 1 FROM trade_executions e WHERE e.trade_id = t.agent_trade_id AND e.source = 'BACKFILL'
                AND e.event = 'ENTRY_FILL')
   AND NOT EXISTS (SELECT 1 FROM trade_executions e WHERE e.trade_id = t.agent_trade_id
                    AND e.event = 'PARTIAL_EXIT');

-- paper_trades: final exit. pnl left NULL: older rows hold last-leg-only P&L, newer ones the whole trade.
INSERT INTO trade_executions (occurred_at, instrument, segment, is_paper, trade_id, symbol, strategy, side,
                              event, quantity, price, reason, source, detail)
SELECT t.exit_timestamp, 'EQUITY', CASE WHEN upper(coalesce(t.product, '')) IN ('MIS', 'INTRADAY') THEN 'INTRADAY' ELSE 'SWING' END, true,
       t.agent_trade_id, t.symbol, t.strategy_name, t.direction, 'FINAL_EXIT',
       t.quantity, t.exit_price, t.exit_reason, 'BACKFILL',
       jsonb_build_object('row_pnl_realized', t.pnl_realized, 'partial_pnl_booked', t.partial_pnl_booked,
                          'original_quantity', t.original_quantity)
  FROM paper_trades t
 WHERE t.order_state = 'CLOSED' AND t.exit_timestamp IS NOT NULL
   AND EXISTS (SELECT 1 FROM trade_executions e WHERE e.trade_id = t.agent_trade_id AND e.source = 'BACKFILL'
                AND e.event = 'ENTRY_FILL')
   AND NOT EXISTS (SELECT 1 FROM trade_executions e WHERE e.trade_id = t.agent_trade_id
                    AND e.event = 'FINAL_EXIT');

-- options_positions: entry, and exit for closed ones.
INSERT INTO trade_executions (occurred_at, instrument, segment, is_paper, trade_id, symbol, strategy,
                              event, source, detail)
SELECT p.opened_at, 'OPTIONS', 'OPTIONS', p.is_paper, p.position_id, p.underlying, p.strategy_type,
       'ENTRY_FILL', 'BACKFILL',
       jsonb_build_object('legs', p.legs, 'expiry', p.expiry, 'net_credit', p.net_credit,
                          'max_loss', p.max_loss, 'max_profit', p.max_profit, 'entry_margin', p.entry_margin)
  FROM options_positions p
 WHERE NOT EXISTS (SELECT 1 FROM trade_executions e WHERE e.trade_id = p.position_id);

INSERT INTO trade_executions (occurred_at, instrument, segment, is_paper, trade_id, symbol, strategy,
                              event, pnl, reason, source, detail)
SELECT p.closed_at, 'OPTIONS', 'OPTIONS', p.is_paper, p.position_id, p.underlying, p.strategy_type,
       'FINAL_EXIT', p.realized_pnl, p.exit_reason, 'BACKFILL',
       jsonb_build_object('legs', p.legs, 'adjustments_count', p.adjustments_count)
  FROM options_positions p
 WHERE p.closed_at IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM trade_executions e WHERE e.trade_id = p.position_id AND e.event = 'FINAL_EXIT');
