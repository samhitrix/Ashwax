"""Unit tests for src/brokers/adapters/paper/fill_simulator.py."""

from __future__ import annotations

from src.brokers.adapters.paper.fill_simulator import check_fill, check_gtt_triggers
from src.brokers.base import OrderRequest


def _req(**kw: object) -> OrderRequest:
    base = dict(
        symbol="INFY",
        exchange="NSE",
        direction="BUY",
        quantity=10,
        order_type="MARKET",
        product="INTRADAY",
        validity="DAY",
        agent_trade_id="AT001",
        signal_id="SIG001",
    )
    base.update(kw)
    return OrderRequest(**base)  # type: ignore[arg-type]


# ── MARKET orders ──────────────────────────────────────────────────────────────


def test_market_buy_fills_with_slippage() -> None:
    filled, fill = check_fill(
        _req(direction="BUY"), ltp=1000.0, order_id="P-001", slippage_bps=10.0
    )
    assert filled is True
    assert fill is not None
    assert fill.price == 1001.0  # 1000 * (1 + 10/10000)
    assert fill.quantity == 10


def test_market_sell_fills_with_slippage() -> None:
    filled, fill = check_fill(
        _req(direction="SELL"), ltp=1000.0, order_id="P-002", slippage_bps=10.0
    )
    assert filled is True
    assert fill is not None
    assert fill.price == 999.0  # 1000 * (1 - 10/10000)


def test_market_fill_sets_correct_order_id() -> None:
    _, fill = check_fill(_req(), ltp=500.0, order_id="PAPER-000001")
    assert fill is not None
    assert fill.order_id == "PAPER-000001"
    assert "PAPER-000001" in fill.trade_id


# ── LIMIT orders ───────────────────────────────────────────────────────────────


def test_limit_buy_fills_when_ltp_at_or_below_price() -> None:
    req = _req(order_type="LIMIT", price=1500.0)
    filled, fill = check_fill(req, ltp=1499.0, order_id="P-003")
    assert filled is True
    assert fill is not None
    assert fill.price == 1500.0  # filled at limit price


def test_limit_buy_does_not_fill_when_ltp_above_price() -> None:
    req = _req(order_type="LIMIT", price=1500.0)
    filled, fill = check_fill(req, ltp=1501.0, order_id="P-004")
    assert filled is False
    assert fill is None


def test_limit_sell_fills_when_ltp_at_or_above_price() -> None:
    req = _req(direction="SELL", order_type="LIMIT", price=1500.0)
    filled, fill = check_fill(req, ltp=1501.0, order_id="P-005")
    assert filled is True
    assert fill is not None
    assert fill.price == 1500.0


def test_limit_sell_does_not_fill_when_ltp_below_price() -> None:
    req = _req(direction="SELL", order_type="LIMIT", price=1500.0)
    filled, fill = check_fill(req, ltp=1499.0, order_id="P-006")
    assert filled is False


# ── SL orders ─────────────────────────────────────────────────────────────────


def test_sl_buy_fills_when_ltp_at_or_above_trigger() -> None:
    req = _req(order_type="SL", trigger_price=1520.0, price=1525.0)
    filled, fill = check_fill(req, ltp=1521.0, order_id="P-007")
    assert filled is True
    assert fill is not None
    assert fill.price == 1525.0


def test_sl_buy_does_not_fill_below_trigger() -> None:
    req = _req(order_type="SL", trigger_price=1520.0, price=1525.0)
    filled, _ = check_fill(req, ltp=1519.0, order_id="P-008")
    assert filled is False


def test_sl_sell_fills_when_ltp_at_or_below_trigger() -> None:
    req = _req(direction="SELL", order_type="SL", trigger_price=1480.0, price=1475.0)
    filled, fill = check_fill(req, ltp=1479.0, order_id="P-009")
    assert filled is True
    assert fill is not None
    assert fill.price == 1475.0


# ── SL-M orders ────────────────────────────────────────────────────────────────


def test_sl_m_buy_fills_at_market_on_trigger() -> None:
    req = _req(order_type="SL-M", trigger_price=1520.0)
    filled, fill = check_fill(req, ltp=1521.0, order_id="P-010", slippage_bps=10.0)
    assert filled is True
    assert fill is not None
    assert fill.price == round(1521.0 * 1.001, 2)


def test_sl_m_sell_fills_at_market_on_trigger() -> None:
    req = _req(direction="SELL", order_type="SL-M", trigger_price=1480.0)
    filled, fill = check_fill(req, ltp=1479.0, order_id="P-011", slippage_bps=10.0)
    assert filled is True
    assert fill is not None
    assert fill.price == round(1479.0 * 0.999, 2)


# ── check_gtt_triggers ─────────────────────────────────────────────────────────


def test_gtt_sl_triggers_when_ltp_at_or_below() -> None:
    assert check_gtt_triggers(sl_trigger=1400.0, target_trigger=1600.0, ltp=1399.0) == "SL"


def test_gtt_target_triggers_when_ltp_at_or_above() -> None:
    assert check_gtt_triggers(sl_trigger=1400.0, target_trigger=1600.0, ltp=1601.0) == "TARGET"


def test_gtt_no_trigger_when_ltp_between() -> None:
    assert check_gtt_triggers(sl_trigger=1400.0, target_trigger=1600.0, ltp=1500.0) is None


def test_gtt_no_target_single_leg() -> None:
    assert check_gtt_triggers(sl_trigger=1400.0, target_trigger=None, ltp=1601.0) is None
    assert check_gtt_triggers(sl_trigger=1400.0, target_trigger=None, ltp=1399.0) == "SL"
