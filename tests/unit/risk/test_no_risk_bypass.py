"""Risk management can never be bypassed for intraday (ORB) execution.

A live signal may only become an order via a PASSING CRM decision. If the risk
engine is unavailable, or it blocks the trade, NOTHING is placed (fail-closed).
Guards against the prior fail-OPEN bug where a missing CRM defaulted to qty=1.

Placement now runs the unified path: lifecycle.place_entry_protection_and_poll
(paper adapter simulates the fill, live hits the broker) + order_monitor.track.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from src.execution.order_lifecycle import OrderState
from src.orchestrator import _execute_intraday_signal


class _Record:
    """Minimal stand-in for the TradeRecord returned by the lifecycle."""

    def __init__(self, qty: int) -> None:
        self.state = OrderState.FILLED
        self.agent_trade_id = "TRD-TEST-1"
        self.fill_price = 100.0
        self.filled_quantity = qty
        self.sl_price = 99.0
        self.target_price = 102.5
        self.sl_order_id = "SL-1"
        self.target_order_id = "TGT-1"


class _Lifecycle:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def place_entry_protection_and_poll(self, **kwargs) -> _Record:
        self.calls.append(kwargs)
        return _Record(int(kwargs["qty"]))


class _OrderMonitor:
    def __init__(self) -> None:
        self.tracked: list = []

    def get_monitored(self) -> list:
        return []

    def track(self, pos) -> None:
        self.tracked.append(pos)


def _sig() -> SimpleNamespace:
    return SimpleNamespace(
        entry_price=100.0,
        sl=99.0,
        target=102.5,
        direction=SimpleNamespace(value="LONG"),
    )


def _base_shared(lifecycle: _Lifecycle, order_monitor: _OrderMonitor) -> dict:
    return {
        "paper_mode": True,
        "lifecycle": lifecycle,
        "order_monitor": order_monitor,
        "news_avoid_symbols": {},
        "g9_llm": None,
        "current_equity": 200000.0,
        # Reconciliation has completed (steady state): the entry path fail-closes
        # on entry until held positions are loaded from the DB/broker at least once.
        "held_symbols_loaded": True,
    }


async def test_no_crm_engine_places_nothing() -> None:
    """Missing risk engine → fail-closed: no order, symbol not marked traded."""
    lc, om = _Lifecycle(), _OrderMonitor()
    shared = _base_shared(lc, om)
    shared["crm"] = None
    shared["crm_ctx"] = None

    await _execute_intraday_signal(
        shared, "orb_intraday_smart", "EICHERMOT", _sig(), datetime.now()
    )

    assert lc.calls == []
    assert om.tracked == []
    assert "EICHERMOT" not in shared.get("intraday_traded", {}).get(
        datetime.now().date().isoformat(), set()
    )


async def test_crm_block_places_nothing() -> None:
    """CRM returns approved=False → no order placed."""
    lc, om = _Lifecycle(), _OrderMonitor()
    shared = _base_shared(lc, om)
    shared["crm"] = SimpleNamespace(
        evaluate=lambda sig, ctx: SimpleNamespace(
            approved=False, final_quantity=0, blocking_gate="G_RISK", blocking_reason="blocked"
        )
    )
    shared["crm_ctx"] = SimpleNamespace(
        current_equity=200000.0,
        llm_bypass=True,
        max_risk_per_trade=1000.0,
        default_risk_pct=0.0,
        max_risk_per_trade_pct_ceiling=0.0,
        mis_leverage=5.0,
        llm_gate_required={},
    )

    await _execute_intraday_signal(
        shared, "orb_intraday_smart", "EICHERMOT", _sig(), datetime.now()
    )

    assert lc.calls == []


async def test_crm_approve_places_with_sized_qty() -> None:
    """CRM approval is the ONLY path to an order; qty comes from the decision."""
    lc, om = _Lifecycle(), _OrderMonitor()
    shared = _base_shared(lc, om)
    shared["crm"] = SimpleNamespace(
        evaluate=lambda sig, ctx: SimpleNamespace(
            approved=True, final_quantity=7, blocking_gate=None, blocking_reason=None
        )
    )
    shared["crm_ctx"] = SimpleNamespace(
        current_equity=200000.0,
        llm_bypass=True,
        max_risk_per_trade=1000.0,
        default_risk_pct=0.0,
        max_risk_per_trade_pct_ceiling=0.0,
        mis_leverage=5.0,
        llm_gate_required={},
    )

    await _execute_intraday_signal(
        shared, "orb_intraday_smart", "EICHERMOT", _sig(), datetime.now()
    )

    assert len(lc.calls) == 1
    assert lc.calls[0]["qty"] == 7
    # the filled position is registered with the shared OrderMonitor
    assert len(om.tracked) == 1


async def test_placement_increments_daily_trade_counter() -> None:
    """A placed intraday trade must bump crm_ctx.new_trades_today, so CRM G7's
    max_new_trades_per_day cap actually enforces (root cause of the 'too many trades' bug)."""
    lc, om = _Lifecycle(), _OrderMonitor()
    shared = _base_shared(lc, om)
    shared["crm"] = SimpleNamespace(
        evaluate=lambda sig, ctx: SimpleNamespace(
            approved=True, final_quantity=7, blocking_gate=None, blocking_reason=None
        )
    )
    ctx = SimpleNamespace(
        current_equity=200000.0,
        llm_bypass=True,
        max_risk_per_trade=1000.0,
        default_risk_pct=0.0,
        max_risk_per_trade_pct_ceiling=0.0,
        mis_leverage=5.0,
        new_trades_today=0,
        llm_gate_required={},
    )
    shared["crm_ctx"] = ctx

    await _execute_intraday_signal(
        shared, "orb_intraday_smart", "EICHERMOT", _sig(), datetime.now()
    )

    assert len(lc.calls) == 1
    assert ctx.new_trades_today == 1  # counter advanced → G7 will block once it reaches the cap
