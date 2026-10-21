-- Migration 089: exec_broker -- which broker HOLDS a position (multi-broker routing, phase 2).
--
-- NOT the existing `broker` column (migration 087): that names the rate card used for charges.
-- exec_broker is the account the order was placed on and the position lives in, so each live
-- strategy can be routed to its own broker and exits/reconciliation go back to the same one.
--
-- NULL means "legacy": every live row written before this migration was placed on Kite, so live
-- rows are backfilled to 'kite' below. Paper rows are never given a broker (nothing was placed).
--
-- The ledger trigger functions from 083 are re-emitted IN FULL with exec_broker added to every
-- INSERT INTO trade_executions. The value is read with to_jsonb(r)->>'exec_broker', never as a
-- direct field of the record: one record variable serves several tables, and a field reference on a
-- table lacking the column would raise inside the LEDGER_ERROR-swallowing handler and silently
-- drop every ledger row. All other branches/handlers are unchanged from 083.
--
-- Backfill noise: like 083's original_quantity backfill, the UPDATEs below touch only exec_broker,
-- which no ledger branch compares (order_state, quantity, prices, SL/target, pnl, legs, status,
-- closed_at), so they write no ledger events. No trigger is disabled.

ALTER TABLE trades             ADD COLUMN IF NOT EXISTS exec_broker TEXT;
ALTER TABLE paper_trades       ADD COLUMN IF NOT EXISTS exec_broker TEXT;
ALTER TABLE options_positions  ADD COLUMN IF NOT EXISTS exec_broker TEXT;
ALTER TABLE options_structures ADD COLUMN IF NOT EXISTS exec_broker TEXT;
ALTER TABLE external_positions ADD COLUMN IF NOT EXISTS exec_broker TEXT;
ALTER TABLE trade_executions   ADD COLUMN IF NOT EXISTS exec_broker TEXT;

CREATE OR REPLACE FUNCTION trade_executions_equity_ledger() RETURNS trigger AS $$
DECLARE
    paper boolean := (TG_TABLE_NAME = 'paper_trades');
    r     record;
    seg   text;
    eb    text;
BEGIN
    IF TG_OP = 'DELETE' THEN r := OLD; ELSE r := NEW; END IF;
    eb := to_jsonb(r)->>'exec_broker';
    seg := CASE WHEN upper(coalesce(r.product, '')) IN ('MIS', 'INTRADAY') THEN 'INTRADAY' ELSE 'SWING' END;
    BEGIN
        IF TG_OP = 'INSERT' THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                          event, quantity, price, new_value, exec_broker)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                    'ORDER_CREATED', r.quantity, r.intended_price, r.order_state, eb);
            IF r.order_state = 'FILLED' THEN
                INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                              event, quantity, price, exec_broker)
                VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                        'ENTRY_FILL', r.quantity, r.fill_price, eb);
            END IF;
            RETURN NULL;
        ELSIF TG_OP = 'DELETE' THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                          event, quantity, price, old_value, detail, exec_broker)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                    'DELETED', r.quantity, r.fill_price, r.order_state, to_jsonb(OLD), eb);
            RETURN NULL;
        END IF;

        -- UPDATE ---------------------------------------------------------------------------
        IF OLD.order_state IS DISTINCT FROM NEW.order_state THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                          event, old_value, new_value, reason, exec_broker)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                    'STATE_CHANGE', OLD.order_state, NEW.order_state, NEW.rejection_reason, eb);
            IF NEW.order_state = 'FILLED' AND OLD.order_state NOT IN ('FILLED', 'CLOSED') THEN
                INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                              event, quantity, price, exec_broker)
                VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                        'ENTRY_FILL', NEW.quantity, NEW.fill_price, eb);
            END IF;
            IF NEW.order_state = 'CLOSED' THEN
                INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                              event, quantity, price, pnl, reason, detail, exec_broker)
                VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                        'FINAL_EXIT', NEW.quantity, NEW.exit_price,
                        NEW.pnl_realized - COALESCE(NEW.partial_pnl_booked, 0), NEW.exit_reason,
                        jsonb_build_object('trade_total_pnl', NEW.pnl_realized,
                                           'partial_pnl_booked', NEW.partial_pnl_booked,
                                           'original_quantity', NEW.original_quantity), eb);
            END IF;
        END IF;

        -- A partial exit shrinks quantity while the trade stays open.
        IF OLD.order_state = 'FILLED' AND NEW.order_state = 'FILLED' THEN
            IF NEW.quantity < OLD.quantity THEN
                INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                              event, quantity, price, pnl, detail, exec_broker)
                VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                        'PARTIAL_EXIT', OLD.quantity - NEW.quantity, NEW.last_partial_price,
                        NEW.partial_pnl_booked - COALESCE(OLD.partial_pnl_booked, 0),
                        jsonb_build_object('remaining_quantity', NEW.quantity,
                                           'original_quantity', NEW.original_quantity,
                                           'price_recorded', NEW.last_partial_price IS NOT NULL), eb);
            ELSIF NEW.quantity > OLD.quantity THEN
                INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                              event, old_value, new_value, exec_broker)
                VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                        'QTY_CORRECTED', OLD.quantity::text, NEW.quantity::text, eb);
            END IF;
            IF OLD.fill_price IS NOT NULL AND NEW.fill_price IS DISTINCT FROM OLD.fill_price THEN
                INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                              event, old_value, new_value, exec_broker)
                VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                        'FILL_PRICE_CORRECTED', OLD.fill_price::text, NEW.fill_price::text, eb);
            END IF;
        END IF;

        IF NEW.current_sl IS DISTINCT FROM OLD.current_sl THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                          event, old_value, new_value, exec_broker)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                    'SL_MOVE', OLD.current_sl::text, NEW.current_sl::text, eb);
        END IF;
        IF NEW.current_target IS DISTINCT FROM OLD.current_target THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                          event, old_value, new_value, exec_broker)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                    'TARGET_MOVE', OLD.current_target::text, NEW.current_target::text, eb);
        END IF;

        -- Somebody edited a row that was already closed (backfill script, manual SQL, a late writer).
        IF OLD.order_state = 'CLOSED' AND NEW.order_state = 'CLOSED'
           AND (NEW.pnl_realized IS DISTINCT FROM OLD.pnl_realized
                OR NEW.exit_price IS DISTINCT FROM OLD.exit_price
                OR NEW.quantity IS DISTINCT FROM OLD.quantity
                OR NEW.partial_qty_booked IS DISTINCT FROM OLD.partial_qty_booked
                OR NEW.partial_pnl_booked IS DISTINCT FROM OLD.partial_pnl_booked) THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, side,
                                          event, detail, exec_broker)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, r.symbol, r.strategy_name, r.direction,
                    'CLOSED_ROW_EDITED',
                    jsonb_build_object('pnl_realized', jsonb_build_array(OLD.pnl_realized, NEW.pnl_realized),
                                       'exit_price', jsonb_build_array(OLD.exit_price, NEW.exit_price),
                                       'quantity', jsonb_build_array(OLD.quantity, NEW.quantity),
                                       'partial_qty_booked',
                                       jsonb_build_array(OLD.partial_qty_booked, NEW.partial_qty_booked),
                                       'partial_pnl_booked',
                                       jsonb_build_array(OLD.partial_pnl_booked, NEW.partial_pnl_booked)), eb);
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'trade_executions ledger write failed for %: %', r.agent_trade_id, SQLERRM;
        BEGIN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, event, detail, exec_broker)
            VALUES ('EQUITY', seg, paper, r.agent_trade_id, 'LEDGER_ERROR',
                    jsonb_build_object('error', SQLERRM, 'op', TG_OP, 'table', TG_TABLE_NAME), eb);
        EXCEPTION WHEN OTHERS THEN NULL;
        END;
    END;
    RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION trade_executions_options_ledger() RETURNS trigger AS $$
DECLARE
    r  record;
    eb text;
BEGIN
    IF TG_OP = 'DELETE' THEN r := OLD; ELSE r := NEW; END IF;
    eb := to_jsonb(r)->>'exec_broker';
    BEGIN
        IF TG_OP = 'INSERT' THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy,
                                          event, new_value, detail, exec_broker)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type,
                    'ENTRY_FILL', r.status,
                    jsonb_build_object('legs', r.legs, 'expiry', r.expiry, 'net_credit', r.net_credit,
                                       'max_loss', r.max_loss, 'max_profit', r.max_profit,
                                       'entry_margin', r.entry_margin, 'lot_size', r.lot_size_at_entry,
                                       'entry_spot', r.entry_spot), eb);
            RETURN NULL;
        ELSIF TG_OP = 'DELETE' THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy,
                                          event, old_value, detail, exec_broker)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type,
                    'DELETED', r.status, to_jsonb(OLD), eb);
            RETURN NULL;
        END IF;

        IF NEW.legs IS DISTINCT FROM OLD.legs THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy,
                                          event, detail, exec_broker)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type,
                    'ADJUSTMENT',
                    jsonb_build_object('old_legs', OLD.legs, 'new_legs', NEW.legs,
                                       'adjustments_count', NEW.adjustments_count), eb);
        END IF;
        IF NEW.was_breached IS TRUE AND OLD.was_breached IS DISTINCT FROM TRUE THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy, event, exec_broker)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type, 'BREACHED', eb);
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy,
                                          event, old_value, new_value, reason, exec_broker)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type,
                    'STATE_CHANGE', OLD.status, NEW.status, NEW.exit_reason, eb);
        END IF;
        IF OLD.closed_at IS NULL AND NEW.closed_at IS NOT NULL THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy,
                                          event, occurred_at, pnl, reason, detail, exec_broker)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type,
                    'FINAL_EXIT', NEW.closed_at, NEW.realized_pnl, NEW.exit_reason,
                    jsonb_build_object('legs', NEW.legs, 'adjustments_count', NEW.adjustments_count), eb);
        ELSIF OLD.closed_at IS NOT NULL
              AND (NEW.realized_pnl IS DISTINCT FROM OLD.realized_pnl
                   OR NEW.closed_at IS DISTINCT FROM OLD.closed_at) THEN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, symbol, strategy,
                                          event, detail, exec_broker)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, r.underlying, r.strategy_type,
                    'CLOSED_ROW_EDITED',
                    jsonb_build_object('realized_pnl', jsonb_build_array(OLD.realized_pnl, NEW.realized_pnl),
                                       'closed_at', jsonb_build_array(OLD.closed_at, NEW.closed_at)), eb);
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'trade_executions options ledger write failed for %: %', r.position_id, SQLERRM;
        BEGIN
            INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, event, detail, exec_broker)
            VALUES ('OPTIONS', 'OPTIONS', r.is_paper, r.position_id, 'LEDGER_ERROR',
                    jsonb_build_object('error', SQLERRM, 'op', TG_OP), eb);
        EXCEPTION WHEN OTHERS THEN NULL;
        END;
    END;
    RETURN NULL;
END $$ LANGUAGE plpgsql;

-- Backfill: every live row so far went through Kite. Paper rows stay NULL.
UPDATE trades
   SET exec_broker = 'kite'
 WHERE exec_broker IS NULL;
UPDATE options_positions
   SET exec_broker = 'kite'
 WHERE exec_broker IS NULL AND is_paper IS NOT TRUE;

-- Ledger rows inherit the broker of the trade row they belong to, where joinable (live rows only:
-- paper trade rows carry no broker, so nothing is copied for them). Fills a NULL column; no ledger
-- row's existing facts are changed.
UPDATE trade_executions e
   SET exec_broker = t.exec_broker
  FROM trades t
 WHERE e.exec_broker IS NULL AND e.instrument = 'EQUITY' AND e.is_paper IS NOT TRUE
   AND e.trade_id = t.agent_trade_id AND t.exec_broker IS NOT NULL;
UPDATE trade_executions e
   SET exec_broker = p.exec_broker
  FROM options_positions p
 WHERE e.exec_broker IS NULL AND e.instrument = 'OPTIONS' AND e.is_paper IS NOT TRUE
   AND e.trade_id = p.position_id AND p.exec_broker IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_trades_exec_broker_state ON trades (exec_broker, order_state);
CREATE INDEX IF NOT EXISTS idx_trade_executions_exec_broker ON trade_executions (exec_broker);
