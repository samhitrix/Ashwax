"""Unit tests for src/llm/sanity_watchdog.py"""

from __future__ import annotations

from src.llm.sanity_watchdog import (
    SignalOutcome,
    WatchdogVerdict,
    evaluate_llm_sanity,
)


def _app(won: bool) -> SignalOutcome:
    return SignalOutcome(was_approved=True, would_have_won=won)


def _rej(won: bool) -> SignalOutcome:
    return SignalOutcome(was_approved=False, would_have_won=won)


def test_insufficient_data():
    outcomes = [_app(True)] * 3 + [_rej(False)] * 3
    result = evaluate_llm_sanity(outcomes)
    assert result.verdict == WatchdogVerdict.INSUFFICIENT_DATA
    assert result.bypass_llm is False


def test_llm_adding_value():
    approved = [_app(True)] * 8 + [_app(False)] * 2
    rejected = [_rej(True)] * 4 + [_rej(False)] * 6
    result = evaluate_llm_sanity(approved + rejected, rolling_window=20)
    assert result.verdict == WatchdogVerdict.LLM_ADDING_VALUE
    assert result.bypass_llm is False


def test_llm_hurting_alpha():
    approved = [_app(True)] * 4 + [_app(False)] * 6
    rejected = [_rej(True)] * 8 + [_rej(False)] * 2
    result = evaluate_llm_sanity(approved + rejected, rolling_window=20)
    assert result.verdict == WatchdogVerdict.LLM_HURTING_ALPHA
    assert result.bypass_llm is True


def test_empty_outcomes():
    result = evaluate_llm_sanity([])
    assert result.verdict == WatchdogVerdict.INSUFFICIENT_DATA


def test_validate_llm_response_salvages_fenced_json():
    """G9 regression 2026-07-09: LLMs wrap JSON in markdown fences / preamble;
    the gate must salvage the object instead of fail-opening every check."""
    from src.llm.sanity_watchdog import validate_llm_response

    fenced = '```json\n{"confidence": 0.8, "reasoning": "strong breakout"}\n```'
    res = validate_llm_response(fenced)
    assert res.valid is True

    preamble = (
        'Here is my analysis: {"confidence": 0.4, "reasoning": "weak volume"} hope this helps'
    )
    assert validate_llm_response(preamble).valid is True

    # genuinely malformed still fails
    assert validate_llm_response("I cannot answer that").valid is False
