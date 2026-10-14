"""
Regression tests for src/execution/trade_meta.py.

These tests are the structural barrier against the recurring "strategy/regime
blank in dashboard" bug.  They must stay green after any refactor that touches
meta construction or _persist_entry.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.brokers.base import BrokerInterface, Fill, Order
from src.execution.order_lifecycle import OrderLifecycleManager
from src.execution.trade_meta import TradeMetadata, build_trade_meta

# ── build_trade_meta ───────────────────────────────────────────────────────────


def test_build_trade_meta_required_fields_present() -> None:
    m = build_trade_meta(strategy_name="orb_intraday_smart", regime_at_entry="BULL")
    assert m["strategy_name"] == "orb_intraday_smart"
    assert m["regime_at_entry"] == "BULL"


def test_build_trade_meta_no_missing_fields() -> None:
    m = build_trade_meta(strategy_name="orb_intraday_smart", regime_at_entry="BULL")
    assert m.missing_fields() == set()


def test_build_trade_meta_with_llm_verdict() -> None:
    m = build_trade_meta(
        strategy_name="orb_intraday_smart",
        regime_at_entry="BULL",
        llm_confidence=0.90,
        llm_flag="CONFIRM",
        llm_reason="strong breakout",
        llm_evaluated=True,
    )
    assert m["llm_confidence"] == pytest.approx(0.90)
    assert m["llm_flag"] == "CONFIRM"
    assert m["llm_reason"] == "strong breakout"
    assert m["llm_evaluated"] is True


def test_build_trade_meta_is_valid_dict() -> None:
    """build_trade_meta returns something usable as a plain dict."""
    m = build_trade_meta(strategy_name="s", regime_at_entry="r")
    assert isinstance(m, dict)
    assert m.get("strategy_name") == "s"


# ── TradeMetadata.missing_fields ───────────────────────────────────────────────


def test_missing_fields_empty_dict() -> None:
    m = TradeMetadata()
    assert "strategy_name" in m.missing_fields()
    assert "regime_at_entry" in m.missing_fields()


def test_missing_fields_partial() -> None:
    m = TradeMetadata(strategy_name="orb")
    missing = m.missing_fields()
    assert "regime_at_entry" in missing
    assert "strategy_name" not in missing


def test_missing_fields_complete() -> None:
    m = TradeMetadata(strategy_name="orb", regime_at_entry="BULL")
    assert m.missing_fields() == set()


# ── _persist_entry warning when meta is incomplete ─────────────────────────────

_NOW_IST = None  # filled in dynamically


def _make_filled_broker() -> MagicMock:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 1, 15, 9, 30, 0, tzinfo=_IST)
    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(side_effect=["E001", "SL001", "TGT001"])
    broker.get_order_status = AsyncMock(
        return_value=Order(
            broker_order_id="E001",
            symbol="INFY",
            direction="BUY",
            quantity=10,
            filled_quantity=10,
            pending_quantity=0,
            status="FILLED",
            order_type="MARKET",
            product="INTRADAY",
            average_price=1500.0,
            tag="",
            placed_at=now,
        )
    )
    broker.get_trades = AsyncMock(
        return_value=[
            Fill(order_id="E001", trade_id="T1", quantity=10, price=1500.0, exchange_time=now)
        ]
    )
    broker.modify_order = AsyncMock(return_value="OK")
    return broker


@pytest.mark.asyncio  # type: ignore[misc]
async def test_persist_entry_warns_when_strategy_name_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If meta omits strategy_name, _persist_entry emits a WARNING so logs catch it."""
    broker = _make_filled_broker()
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_fill = AsyncMock(return_value=None)
    mgr = OrderLifecycleManager(broker, trade_repo=repo, is_paper=True)

    with caplog.at_level(logging.WARNING, logger="src.execution.order_lifecycle"):
        await mgr.place_entry_protection_and_poll(
            signal_id="SIG1",
            symbol="INFY",
            direction="BUY",
            qty=10,
            entry_price=1500.0,
            sl_price=1450.0,
            target_price=1600.0,
            product="MIS",
            meta={"regime_at_entry": "BULL"},  # strategy_name missing
        )

    assert any("trade_meta_incomplete" in m for m in caplog.messages)
    assert any("strategy_name" in m for m in caplog.messages)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_persist_entry_writes_requested_product() -> None:
    """Regression: _persist_entry used to drop the caller's product entirely —
    the DB row column defaulted to 'MIS' regardless of what was requested, so
    CNC (swing) trades were silently persisted as MIS and became eligible for
    the intraday-only same-day flatten sweep (square_off_open). Found 2026-07-29
    after PARADEEP/RADICO swing fills got flattened as EXIT_WINDOW_FLAT minutes
    after entry."""
    broker = _make_filled_broker()
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_fill = AsyncMock(return_value=None)
    mgr = OrderLifecycleManager(broker, trade_repo=repo, is_paper=True)

    await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="RADICO",
        direction="BUY",
        qty=2,
        entry_price=4443.2,
        sl_price=4087.74,
        target_price=5154.11,
        product="CNC",
        meta={"strategy_name": "oneil_canslim", "regime_at_entry": "BULL"},
    )

    row = repo.insert.call_args.args[0]
    assert row["product"] == "CNC"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_persist_entry_no_warning_when_meta_complete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """build_trade_meta → no warning emitted."""
    broker = _make_filled_broker()
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_fill = AsyncMock(return_value=None)
    mgr = OrderLifecycleManager(broker, trade_repo=repo, is_paper=True)

    with caplog.at_level(logging.WARNING, logger="src.execution.order_lifecycle"):
        await mgr.place_entry_protection_and_poll(
            signal_id="SIG1",
            symbol="INFY",
            direction="BUY",
            qty=10,
            entry_price=1500.0,
            sl_price=1450.0,
            target_price=1600.0,
            product="MIS",
            meta=build_trade_meta(
                strategy_name="orb_intraday_smart",
                regime_at_entry="BULL",
            ),
        )

    assert not any("trade_meta_incomplete" in m for m in caplog.messages)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_build_trade_meta_all_fields_land_in_db_row() -> None:
    """End-to-end: build_trade_meta → place_entry_protection_and_poll → INSERT row
    must contain strategy_name and regime_at_entry.  This is the regression test for
    the 'strategy/regime blank in dashboard' bug."""
    broker = _make_filled_broker()
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_fill = AsyncMock(return_value=None)
    mgr = OrderLifecycleManager(broker, trade_repo=repo, is_paper=True)

    await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="MIS",
        meta=build_trade_meta(
            strategy_name="orb_intraday_smart",
            regime_at_entry="BULL",
            llm_confidence=0.90,
            llm_flag="CONFIRM",
            llm_reason="breakout confirmed",
        ),
    )

    row = repo.insert.await_args.args[0]
    assert row["strategy_name"] == "orb_intraday_smart", "strategy_name must be in DB row"
    assert row["regime_at_entry"] == "BULL", "regime_at_entry must be in DB row"
    assert float(row["llm_confidence"]) == pytest.approx(0.90)
