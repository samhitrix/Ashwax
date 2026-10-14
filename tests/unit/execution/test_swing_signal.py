"""Tests for the DAILY-strategy swing signal -> CNC order path
(src/orchestrator.py: _swing_signal_scan_loop, _execute_swing_signal).

Regression coverage for the bugs found 2026-07-29 that made this path place
zero orders since 2026-07-15: symbol-prefix mismatch on the OHLCV fetch,
wrong place_entry_protection_and_poll() signature (missing signal_id, invalid
strategy_name/is_paper kwargs), wrong TradeRecord field access (.trade_id vs
.agent_trade_id), wrong OrderMonitor.track() call shape, and wrong CRMDecision
field access (.approved_quantity/.blocked_by vs .final_quantity/.blocking_gate).
"""

from __future__ import annotations

import asyncio
import datetime as datetime_module
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
import src.orchestrator as orch_mod
from src.execution.order_lifecycle import OrderState
from src.orchestrator import _execute_swing_signal

_IST = ZoneInfo("Asia/Kolkata")
_NOW = datetime(2026, 7, 29, 16, 0, tzinfo=_IST)  # Wednesday, AMO-window open (market_hours.json)
_SIG = SimpleNamespace(
    entry_price=100.0, sl=95.0, target=110.0, direction=SimpleNamespace(value="LONG")
)


@pytest.fixture(autouse=True)
def _silence_orchestrator_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """orchestrator._log is a module-level logger writing to the REAL logs/
    directory regardless of test context. Every test in this file drives real
    orchestrator functions (_execute_swing_signal / _swing_signal_scan_loop),
    so without this they inject fake swing_crm_blocked / swing_scan_start
    entries into the real logs/orchestrator.log on every test run — this
    happened and was mistaken for the live system misbehaving."""
    noop_log = logging.getLogger("test_swing_signal_noop")
    noop_log.addHandler(logging.NullHandler())
    noop_log.propagate = False
    monkeypatch.setattr(orch_mod, "_log", noop_log)
    monkeypatch.setattr(orch_mod, "_swing_log", noop_log)


def _with_cash(lifecycle: AsyncMock, cash: float = 200_000.0) -> AsyncMock:
    """Attach a broker whose get_margins() reports real cash.

    Without this the AsyncMock auto-child's available_cash is a MagicMock, and
    MagicMock.__float__ returns 1.0 — so the delivery-cash gate correctly sees Rs 1
    and blocks every order. Test-double artefact, not a product bug.
    """
    from src.brokers.base import Margin

    broker = AsyncMock()
    broker.get_margins.return_value = Margin(
        equity=cash, available_cash=cash, used_margin=0.0, collateral=0.0
    )
    lifecycle._broker = broker
    return lifecycle


def _shared(**overrides: object) -> dict[str, object]:
    lifecycle = AsyncMock()
    lifecycle.place_entry_protection_and_poll.return_value = SimpleNamespace(
        agent_trade_id="TRD-ABC123",
        state=OrderState.FILLED,
        fill_price=100.5,
        filled_quantity=10,
        sl_price=95.0,
        target_price=110.0,
        sl_order_id="SL1",
        target_order_id="TG1",
        # Swing protection is one two-leg GTT; the id must reach MonitoredPosition
        # or trailing and the 30s GTT integrity check have nothing to act on.
        gtt_sl_id="GTT-1",
    )
    _with_cash(lifecycle)
    crm = SimpleNamespace(
        evaluate=lambda sig, ctx: SimpleNamespace(
            approved=True,
            final_quantity=10,
            blocking_gate="",
            blocking_reason="",
            gate_results=[],
        )
    )
    crm_ctx = SimpleNamespace(
        current_equity=200_000.0,
        max_risk_per_trade=2_000.0,
        default_risk_pct=0.0,
        max_risk_per_trade_pct_ceiling=0.0,
        llm_gate_required={},
        blocked_regimes={},
    )
    order_monitor = MagicMock()
    order_monitor.get_monitored.return_value = []
    base: dict[str, object] = dict(
        lifecycle=lifecycle,
        crm=crm,
        crm_ctx=crm_ctx,
        order_monitor=order_monitor,
        held_symbols_loaded=True,
        held_symbols=set(),
        available_cash=200_000.0,
        regime="BULL",
        health_scores={},
        news_avoid_symbols={},
        news_high_magnitude={},
        vix=15.0,
        autonomy_mode="autonomous",
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio  # type: ignore[misc]
async def test_execute_swing_signal_calls_lifecycle_with_correct_signature() -> None:
    shared = _shared()
    trade_id, price, sl, target, reason, gate_results = await _execute_swing_signal(
        shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW
    )
    assert reason == "executed"
    assert gate_results == []
    assert trade_id == "TRD-ABC123"
    call_kwargs = shared["lifecycle"].place_entry_protection_and_poll.call_args.kwargs  # type: ignore[attr-defined]
    assert call_kwargs["signal_id"].startswith("SWING-NSE:RELIANCE-")
    assert "strategy_name" not in call_kwargs
    assert "is_paper" not in call_kwargs
    assert call_kwargs["product"] == "CNC"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_execute_swing_signal_tracks_monitored_position_on_fill() -> None:
    shared = _shared()
    await _execute_swing_signal(shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW)
    shared["order_monitor"].track.assert_called_once()  # type: ignore[attr-defined]
    pos = shared["order_monitor"].track.call_args.args[0]  # type: ignore[attr-defined]
    assert pos.agent_trade_id == "TRD-ABC123"
    assert pos.fill_price == 100.5
    assert pos.product == "CNC"
    # Both columns carry the one OCO trigger id — update_sl trails it, and the 30s
    # integrity check re-arms it if it vanishes at the broker.
    assert pos.gtt_sl_id == "GTT-1"
    assert pos.gtt_target_id == "GTT-1"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_execute_swing_signal_reports_crm_block_reason() -> None:
    shared = _shared(
        crm=SimpleNamespace(
            evaluate=lambda sig, ctx: SimpleNamespace(
                approved=False,
                final_quantity=0,
                blocking_gate="G3",
                blocking_reason="closed",
                gate_results=[
                    SimpleNamespace(
                        gate_id="G0", decision=SimpleNamespace(value="PASS"), reason=""
                    ),
                    SimpleNamespace(
                        gate_id="G3", decision=SimpleNamespace(value="BLOCK"), reason="closed"
                    ),
                ],
            )
        )
    )
    trade_id, _price, _sl, _target, reason, gate_results = await _execute_swing_signal(
        shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW
    )
    assert trade_id is None
    assert reason == "crm_blocked:G3"
    assert gate_results == [
        {"gate_id": "G0", "decision": "PASS", "reason": ""},
        {"gate_id": "G3", "decision": "BLOCK", "reason": "closed"},
    ]
    shared["lifecycle"].place_entry_protection_and_poll.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_swing_scan_fetches_ohlcv_with_bare_symbol(tmp_path: Path) -> None:
    """Regression: the scan used to prefix the fetch with 'NSE:', which never
    matched the bare symbols stored in ohlcv_candles, so daily_frames stayed
    empty and no DAILY strategy was ever evaluated (symbols=0 every day).

    IMPORTANT: this drives the real _swing_signal_scan_loop, which writes to
    module-level _LOGS/"swing_signals.json" AND logs through the module-level
    shared `_log` (real orchestrator logger, writing to the real logs/
    directory regardless of test context) — both are monkeypatched below so
    the test can never pollute the real dashboard file or orchestrator.log
    (a prior version of this test did both, injecting fake 2-symbol
    "swing_scan_start" entries into the real log on every test run)."""

    class _FixedDatetime(datetime_module.datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime_module.datetime:  # noqa: ANN401
            return _NOW

    fetch_calls: list[str] = []

    async def fake_fetch(symbol: str, timeframe: str, limit: int) -> None:
        fetch_calls.append(symbol)
        return

    fake_strategy = SimpleNamespace(
        spec=SimpleNamespace(meta=SimpleNamespace(name="minervini_vcp", timeframe="DAILY")),
        set_market_context=lambda market_df: None,
        set_rs_percentile_override=lambda value: None,
    )
    stop = asyncio.Event()

    async def fake_sleep(_seconds: float) -> None:
        stop.set()

    shared = {
        "strategy_registry": SimpleNamespace(all_enabled=lambda: [fake_strategy]),
        "ohlcv_repo": SimpleNamespace(fetch_dataframe=fake_fetch),
        "watchlist": ["RELIANCE", "TCS"],
    }

    orig_sleep = asyncio.sleep
    orig_logs = orch_mod._LOGS
    try:
        datetime_module.datetime = _FixedDatetime  # type: ignore[misc]
        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        orch_mod._LOGS = tmp_path  # type: ignore[assignment]
        await orch_mod._swing_signal_scan_loop(shared, stop)
    finally:
        datetime_module.datetime = datetime  # type: ignore[misc]
        asyncio.sleep = orig_sleep
        orch_mod._LOGS = orig_logs

    # "NIFTY 50" is fetched once up front (benchmark for market context + the
    # cross-sectional RS override), before the strategy universe — all bare
    # symbols, none "NSE:"-prefixed.
    assert fetch_calls == ["NIFTY 50", "RELIANCE", "TCS"]


# ── Per-segment live routing (2026-08-27) ──────────────────────────────────────
# config/global.json system.segments.swing.paper_mode=false means a swing CNC order
# must be placed through the LIVE stack. shared["stacks"] is the only thing that
# carries the per-mode stacks to this function; it was read in four places and never
# assigned anywhere, so _stacks was always {} and every swing entry silently fell back
# to shared["lifecycle"] — the paper stack — and got a PAPER-xxxxxx order id.


def _two_stacks() -> tuple[dict[str, object], AsyncMock, AsyncMock]:
    def _mk() -> AsyncMock:
        lc = AsyncMock()
        lc.place_entry_protection_and_poll.return_value = SimpleNamespace(
            agent_trade_id="TRD-ABC123",
            state=OrderState.FILLED,
            fill_price=100.5,
            filled_quantity=10,
            sl_price=95.0,
            target_price=110.0,
            sl_order_id="SL1",
            target_order_id="TG1",
        )
        return _with_cash(lc)

    paper_lc, live_lc = _mk(), _mk()
    stacks = {
        "paper": {"lifecycle": paper_lc, "order_monitor": MagicMock()},
        "live": {"lifecycle": live_lc, "order_monitor": MagicMock()},
    }
    for _s in stacks.values():
        _s["order_monitor"].get_monitored.return_value = []  # type: ignore[attr-defined]
    return stacks, paper_lc, live_lc


@pytest.mark.asyncio  # type: ignore[misc]
async def test_swing_live_segment_places_through_live_stack() -> None:
    stacks, paper_lc, live_lc = _two_stacks()
    shared = _shared(stacks=stacks, paper_mode=True)
    shared["crm_ctx"].segment_paper_mode = {"swing": False}  # type: ignore[attr-defined]

    await _execute_swing_signal(shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW)

    live_lc.place_entry_protection_and_poll.assert_awaited_once()
    paper_lc.place_entry_protection_and_poll.assert_not_awaited()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_swing_paper_segment_places_through_paper_stack() -> None:
    stacks, paper_lc, live_lc = _two_stacks()
    shared = _shared(stacks=stacks, paper_mode=False)
    shared["crm_ctx"].segment_paper_mode = {"swing": True}  # type: ignore[attr-defined]

    await _execute_swing_signal(shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW)

    paper_lc.place_entry_protection_and_poll.assert_awaited_once()
    live_lc.place_entry_protection_and_poll.assert_not_awaited()


def test_orchestrator_shared_state_exposes_stacks() -> None:
    """The shared dict literal must carry `stacks` — without it the two tests above
    pass in isolation while the real system never routes anything to live."""
    import inspect

    src = inspect.getsource(orch_mod)
    assert '"stacks": stacks,' in src, "shared state never exposes the per-mode stacks"


# ── Delivery cash gate (2026-08-27) ────────────────────────────────────────────
# CNC is delivery: paid in full, in cash. Pledged collateral funds intraday/F&O margin
# but cannot buy delivery stock. On 2026-08-27 three swing orders totalling Rs 36,706
# went out against Rs 29,785 of real cash because nothing checked before placing.


@pytest.mark.asyncio  # type: ignore[misc]
async def test_swing_blocked_when_cash_below_order_value() -> None:
    shared = _shared()
    _with_cash(shared["lifecycle"], cash=50.0)  # type: ignore[arg-type]  # < 1 share at 100

    tid, _, _, _, reason, _ = await _execute_swing_signal(
        shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW
    )

    assert tid is None
    assert reason == "insufficient_cash"
    shared["lifecycle"].place_entry_protection_and_poll.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_swing_cash_gate_reads_broker_live_before_each_order() -> None:
    """Orders go out one at a time and cash is re-read before every one, so order 2
    sees what order 1 actually consumed rather than a stale snapshot."""
    shared = _shared()
    _with_cash(shared["lifecycle"], cash=200_000.0)  # type: ignore[arg-type]

    await _execute_swing_signal(shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW)
    await _execute_swing_signal(shared, "minervini_vcp", "NSE:TCS", _SIG, _NOW)

    assert shared["lifecycle"]._broker.get_margins.await_count == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_swing_cash_gate_blocks_second_order_when_live_read_is_stale() -> None:
    """Regression 2026-09-07: minervini_vcp and oneil_canslim both signalled CGCL in
    the same 15:45 AMO scan. Zerodha's margin API does not reserve cash for a
    pending AMO until it executes next session, so get_margins() legitimately
    returns the SAME full cash before both orders — order 2 must still be
    blocked by the session-committed accumulator, not just by a live re-read
    that cannot see order 1's not-yet-executed AMO."""
    shared = _shared()
    # One full order (10 x 100 = 1000) fits; the 50 left afterwards is < 1 share, so
    # order 2 cannot be sized down either and must still be blocked.
    _with_cash(shared["lifecycle"], cash=1_050.0)

    tid1, *_rest1, reason1, _ = await _execute_swing_signal(
        shared, "minervini_vcp", "NSE:CGCL", _SIG, _NOW
    )
    tid2, *_rest2, reason2, _ = await _execute_swing_signal(
        shared, "oneil_canslim", "NSE:CGCL", _SIG, _NOW
    )

    assert tid1 is not None
    assert reason1 == "executed"
    assert tid2 is None
    assert reason2 == "insufficient_cash"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_swing_cash_gate_honours_cushion() -> None:
    """A cushion holds back a fraction of cash so the account is never spent to zero.
    Where the full order doesn't fit, it is sized DOWN to what is spendable (same SL,
    fewer shares = less risk) instead of being skipped."""
    shared = _shared(swing_cash_cushion_pct=0.5)
    _with_cash(shared["lifecycle"], cash=1_500.0)  # type: ignore[arg-type]
    # order value = 10 x 100 = 1000; spendable = 1500 x 0.5 = 750 -> reduced to 7 shares

    _, _, _, _, reason, _ = await _execute_swing_signal(
        shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW
    )

    assert reason == "executed"
    placed_qty = shared["lifecycle"].place_entry_protection_and_poll.await_args.kwargs["qty"]  # type: ignore[attr-defined]
    assert placed_qty == 7


# ── G9 LLM confirmation on the swing path (2026-09-10) ────────────────────────
# Swing entries bypassed G9 entirely: meta["llm_evaluated"] was set only in
# _execute_intraday_signal and _crm_loop, so crm.py's G9 always took its
# fail-open "[no-verdict]" branch. Every CNC trade ever placed (42 rows across
# paper_trades + trades) had llm_flag NULL, while 273/423 MIS rows had one.
# G9 now runs here for strategies whose registry llm_gate_required is true, and
# FAILS CLOSED for them: an LLM-originated idea whose independent reviewer is
# unavailable has no validation at all, so it must not trade. The REJECT /
# confidence-floor blocking itself lives in crm.py's G9 and is covered by
# tests/unit/risk/test_crm.py — these cover the wiring into the swing path.


def _gated(**required: bool) -> SimpleNamespace:
    """crm_ctx where the registry's llm_gate_required marks `required` strategies."""
    return SimpleNamespace(
        current_equity=200_000.0,
        max_risk_per_trade=2_000.0,
        default_risk_pct=0.0,
        max_risk_per_trade_pct_ceiling=0.0,
        llm_gate_required=dict(required),
        blocked_regimes={},
    )


def _verdict(
    flag: str = "CONFIRM", conf: float = 0.80, evaluated: bool = True
) -> dict[str, object]:
    return {
        "llm_confidence": conf,
        "llm_flag": flag,
        "llm_reason": "test reason",
        "malformed": 0,
        "latency_ms": 12,
        "evaluated": evaluated,
    }


def _patch_evaluate(
    monkeypatch: pytest.MonkeyPatch, verdict: dict[str, object]
) -> list[dict[str, object]]:
    """Capture the G9 payload and return a canned verdict."""
    calls: list[dict[str, object]] = []

    async def _fake(
        client: object,
        sig: dict[str, object],
        consecutive_malformed: int = 0,
        chain_timeout_secs: float | None = None,
    ) -> dict[str, object]:
        calls.append(sig)
        return verdict

    import src.llm.trade_evaluator as _te

    monkeypatch.setattr(_te, "evaluate_trade_gated", _fake)
    return calls


def _capturing_crm() -> tuple[MagicMock, dict[str, object]]:
    """CRM double that records the CRMSignal it was handed."""
    captured: dict[str, object] = {}

    def _evaluate(sig: object, ctx: object) -> object:
        captured["sig"] = sig
        return SimpleNamespace(
            approved=True,
            final_quantity=10,
            blocking_gate="",
            blocking_reason="",
            gate_results=[],
        )

    crm = MagicMock()
    crm.evaluate.side_effect = _evaluate
    return crm, captured


@pytest.mark.asyncio  # type: ignore[misc]
async def test_swing_g9_populates_llm_meta_for_listed_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crm, captured = _capturing_crm()
    shared = _shared(swing_g9_llm=MagicMock(), crm=crm, crm_ctx=_gated(llm_swing_agent=True))
    calls = _patch_evaluate(monkeypatch, _verdict())

    _, _, _, _, reason, _ = await _execute_swing_signal(
        shared, "llm_swing_agent", "NSE:RELIANCE", _SIG, _NOW
    )

    assert reason == "executed"
    assert len(calls) == 1
    # Payload shape matches the intraday G9 call: percentages off the entry.
    assert calls[0]["symbol"] == "NSE:RELIANCE"
    assert calls[0]["strategy_name"] == "llm_swing_agent"
    assert calls[0]["stop_loss_pct"] == pytest.approx(5.0)
    assert calls[0]["target_pct"] == pytest.approx(10.0)

    meta = captured["sig"].meta  # type: ignore[attr-defined]
    assert meta["llm_flag"] == "CONFIRM"
    assert meta["llm_confidence"] == 0.80
    assert meta["llm_reason"] == "test reason"
    assert meta["llm_evaluated"] is True
    # That same meta dict is what order_lifecycle writes llm_flag/llm_confidence
    # from (order_lifecycle.py:902) — this is the DB + dashboard wiring.
    assert (
        shared["lifecycle"].place_entry_protection_and_poll.call_args.kwargs["meta"]  # type: ignore[attr-defined]
        is meta
    )


@pytest.mark.asyncio  # type: ignore[misc]
async def test_swing_g9_reject_verdict_reaches_crm(monkeypatch: pytest.MonkeyPatch) -> None:
    crm, captured = _capturing_crm()
    shared = _shared(swing_g9_llm=MagicMock(), crm=crm, crm_ctx=_gated(llm_swing_agent=True))
    _patch_evaluate(monkeypatch, _verdict(flag="REJECT", conf=0.10))

    await _execute_swing_signal(shared, "llm_swing_agent", "NSE:RELIANCE", _SIG, _NOW)

    meta = captured["sig"].meta  # type: ignore[attr-defined]
    assert meta["llm_flag"] == "REJECT"
    assert meta["llm_evaluated"] is True


@pytest.mark.asyncio  # type: ignore[misc]
async def test_swing_g9_fails_closed_when_reviewer_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crm, _ = _capturing_crm()
    shared = _shared(swing_g9_llm=MagicMock(), crm=crm, crm_ctx=_gated(llm_swing_agent=True))
    _patch_evaluate(monkeypatch, _verdict(flag="NEUTRAL", conf=0.5, evaluated=False))

    trade_id, _, _, _, reason, gates = await _execute_swing_signal(
        shared, "llm_swing_agent", "NSE:RELIANCE", _SIG, _NOW
    )

    assert trade_id is None
    assert reason == "g9_no_verdict"
    assert gates == []
    crm.evaluate.assert_not_called()
    shared["lifecycle"].place_entry_protection_and_poll.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_swing_g9_fails_closed_when_no_provider_available() -> None:
    crm, _ = _capturing_crm()
    # Explicit None = chain resolved and nothing was available (no groq/ollama).
    shared = _shared(swing_g9_llm=None, crm=crm, crm_ctx=_gated(llm_swing_agent=True))

    trade_id, _, _, _, reason, _ = await _execute_swing_signal(
        shared, "llm_swing_agent", "NSE:RELIANCE", _SIG, _NOW
    )

    assert trade_id is None
    assert reason == "g9_no_verdict"
    crm.evaluate.assert_not_called()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_swing_g9_leaves_not_required_strategies_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """llm_gate_required is false for minervini_vcp, so it makes no LLM call and is
    not blocked for lack of a verdict."""
    shared = _shared(swing_g9_llm=MagicMock(), crm_ctx=_gated(minervini_vcp=False))
    calls = _patch_evaluate(monkeypatch, _verdict(evaluated=False))

    _, _, _, _, reason, _ = await _execute_swing_signal(
        shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW
    )

    assert reason == "executed"
    assert calls == []
