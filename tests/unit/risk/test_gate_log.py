"""Tests for the dedicated per-gate decision log (src/risk/gate_log.py -> logs/gates.log).

Before 2026-09-10 nothing wrote a per-gate line anywhere: _execute_swing_signal
logged only the FIRST blocking gate (swing_crm_blocked gate=G4) and an APPROVED
signal produced no gate detail at all, so there was no way to see why a trade
that DID fire passed each gate. gate_results was returned to callers and stored
by the API, but never logged.

This log is deliberately unscoped: every strategy, every segment, every signal
that reaches crm.evaluate(), approved or blocked.
"""

from __future__ import annotations

import logging

import pytest
from src.risk.crm import CRMDecision, CRMSignal, GateDecision, GateResult
from src.risk.gate_log import log_gate_decisions


def _signal(**kw: object) -> CRMSignal:
    base: dict[str, object] = dict(
        signal_id="SWING-NSE:HINDCOPPER-20260910",
        symbol="NSE:HINDCOPPER",
        direction="BUY",
        strategy_name="llm_swing_agent",
        intended_quantity=133,
        stop_loss_pct=2.8,
        target_pct=8.4,
        risk_rupees=15.0,
        engine="swing",
    )
    base.update(kw)
    return CRMSignal(**base)  # type: ignore[arg-type]


def _decision(approved: bool, gates: list[GateResult], **kw: object) -> CRMDecision:
    base: dict[str, object] = dict(
        signal_id="SWING-NSE:HINDCOPPER-20260910",
        approved=approved,
        final_quantity=133 if approved else 0,
        gate_results=gates,
    )
    base.update(kw)
    return CRMDecision(**base)  # type: ignore[arg-type]


@pytest.fixture  # type: ignore[misc]
def gate_log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """caplog.messages rebuilds its list on every access, so it must be read AFTER
    the call under test — returning it from a fixture yields an empty snapshot."""
    caplog.set_level(logging.INFO, logger="gates")
    return caplog


def test_logs_one_line_per_gate_plus_final_on_approval(gate_log: pytest.LogCaptureFixture) -> None:
    log_gate_decisions(
        _signal(),
        _decision(
            True,
            [
                GateResult("G0", GateDecision.PASS),
                GateResult("G4", GateDecision.PASS, "Regime BULL"),
                GateResult("G8", GateDecision.MODIFY, "risk-sized", modified_quantity=133),
            ],
        ),
    )
    assert len(gate_log.messages) == 4  # 3 gates + FINAL
    assert "G0" in gate_log.messages[0] and "PASS" in gate_log.messages[0]
    assert (
        "G8" in gate_log.messages[2]
        and "MODIFY" in gate_log.messages[2]
        and "133" in gate_log.messages[2]
    )
    assert "FINAL" in gate_log.messages[3] and "APPROVED" in gate_log.messages[3]


def test_logs_gate_trail_on_block_too(gate_log: pytest.LogCaptureFixture) -> None:
    """An approved trade is not the only case worth a trail — a block must show
    every gate that ran, not just the blocking one."""
    log_gate_decisions(
        _signal(),
        _decision(
            False,
            [
                GateResult("G0", GateDecision.PASS),
                GateResult("G9", GateDecision.BLOCK, "LLM rejected"),
            ],
            blocking_gate="G9",
            blocking_reason="LLM rejected",
        ),
    )
    assert len(gate_log.messages) == 3
    assert (
        "G9" in gate_log.messages[1]
        and "BLOCK" in gate_log.messages[1]
        and "LLM rejected" in gate_log.messages[1]
    )
    assert (
        "FINAL" in gate_log.messages[2]
        and "BLOCKED" in gate_log.messages[2]
        and "G9" in gate_log.messages[2]
    )


def test_every_line_carries_segment_strategy_and_symbol(gate_log: pytest.LogCaptureFixture) -> None:
    """The log is shared across all segments, so each line must be self-identifying
    and greppable (`grep '| SWING |' gates.log`)."""
    log_gate_decisions(_signal(), _decision(True, [GateResult("G0", GateDecision.PASS)]))
    for line in gate_log.messages:
        assert "SWING" in line
        assert "llm_swing_agent" in line
        assert "HINDCOPPER" in line


@pytest.mark.parametrize(  # type: ignore[misc]
    ("kwargs", "expected"),
    [
        ({"engine": "intraday"}, "INTRADAY"),
        ({"engine": "swing"}, "SWING"),
        ({"engine": "swing", "asset_class": "options"}, "OPTIONS"),
    ],
)
def test_segment_derived_from_signal_not_caller(
    gate_log: pytest.LogCaptureFixture, kwargs: dict[str, object], expected: str
) -> None:
    """Segment comes from crm.segment_of(), so all four crm.evaluate() call sites
    get it right without each passing a hand-written string."""
    log_gate_decisions(_signal(**kwargs), _decision(True, [GateResult("G0", GateDecision.PASS)]))
    assert expected in gate_log.messages[0]


def test_empty_gate_results_still_logs_final(gate_log: pytest.LogCaptureFixture) -> None:
    """Pre-CRM rejections hand back an empty gate list; the FINAL line must still
    appear so the signal is never silently absent from the audit trail."""
    log_gate_decisions(
        _signal(), _decision(False, [], blocking_gate="", blocking_reason="no_risk_engine")
    )
    assert len(gate_log.messages) == 1
    assert "FINAL" in gate_log.messages[0] and "BLOCKED" in gate_log.messages[0]


def test_never_raises_on_malformed_decision(gate_log: pytest.LogCaptureFixture) -> None:
    """Logging must never be able to kill a trade path — it is observability, not
    control flow."""
    log_gate_decisions(_signal(), object())  # type: ignore[arg-type]
