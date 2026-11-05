"""
LLM Sanity Watchdog -- pure arithmetic, zero LLM calls.

Two responsibilities:
1. Win-rate comparison: tracks what would have happened to signals the LLM
   rejected vs. approved. If rejected would-be WR > approved WR, bypass gate.

2. Structural validation: checks every raw LLM response for JSON validity,
   confidence range (float 0.0-1.0), and non-empty reasoning. Tracks consecutive
   malformed responses; 3 in a row triggers fallback-LLM switch; if both LLMs
   malformed, disables gate entirely.

Design constraint (from architecture docs):
  "pure arithmetic, zero LLM calls -- tracks rejected signals would-be
   outcomes via LTP replay. Bypass LLM if approved-trade WR < rejected would-be
   WR over rolling 10 trades."

This module is intentionally stateless -- callers hold the outcome records
and pass them in.  No DB access, no async, no LLM.
"""

from __future__ import annotations

import json as _json
import logging as _logging
from dataclasses import dataclass
from enum import StrEnum

_log = _logging.getLogger(
    "llm.sanity_watchdog"
)  # flat "llm.*" hierarchy, not "src.llm.*" -- see logger.py's own comment on this

_ROLLING_WINDOW = 10
_MIN_SAMPLES_EACH = 5  # need at least this many in each bucket to compare
_MALFORMED_STRIKE_LIMIT = 3  # consecutive malformed → switch to fallback LLM
_DSL_ONLY_RECOVERY_TRADES = 20  # DSL-only trades needed before LLM gate can resume


class WatchdogVerdict(StrEnum):
    LLM_ADDING_VALUE = "LLM_ADDING_VALUE"  # approved WR >= rejected WR
    LLM_HURTING_ALPHA = "LLM_HURTING_ALPHA"  # rejected WR > approved WR -- bypass
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # not enough samples yet


@dataclass
class SignalOutcome:
    """Outcome of a single signal, either approved or rejected by LLM gate."""

    was_approved: bool  # True = LLM let through; False = LLM blocked
    would_have_won: bool  # True = signal direction was correct (LTP moved favourably)


@dataclass
class SanityReport:
    verdict: WatchdogVerdict
    approved_win_rate: float
    rejected_win_rate: float
    approved_sample_count: int
    rejected_sample_count: int
    reason: str
    bypass_llm: bool


@dataclass
class ValidationResult:
    valid: bool
    malformed_count: int  # updated consecutive count after this call
    switch_to_fallback: bool  # True when malformed_count >= _MALFORMED_STRIKE_LIMIT
    disable_gate: bool  # True when called with fallback_already_failed=True and invalid
    reason: str


@dataclass
class LLMRecoveryState:
    """Tracks DSL-only trade count while LLM gate is bypassed."""

    dsl_only_trades: int


def validate_llm_response(
    raw_json: str,
    consecutive_malformed: int = 0,
    fallback_already_failed: bool = False,
) -> ValidationResult:
    """
    Validate a raw LLM response string for structural correctness.

    Checks:
      1. Valid JSON
      2. 'confidence' is a float in [0.0, 1.0]
      3. 'reasoning' is a non-empty string

    Args:
        raw_json: Raw string returned by the LLM provider.
        consecutive_malformed: Number of consecutive malformed responses before
                               this call (caller maintains this counter).
        fallback_already_failed: True if the primary LLM already switched to
                                 fallback and fallback also returned malformed.

    Returns:
        ValidationResult — caller should act on switch_to_fallback / disable_gate.
    """
    try:
        data = _json.loads(raw_json)
    except Exception as exc:
        # LLMs often wrap valid JSON in markdown fences or preamble text
        # ("```json {...} ```", "Here is my analysis: {...}"). Salvage the
        # first {...} block before declaring the response malformed —
        # 2026-07-09 every G9 check failed on exactly this and fail-opened.
        data = None
        _s = (raw_json or "").strip()
        _a, _b = _s.find("{"), _s.rfind("}")
        if _a != -1 and _b > _a:
            try:
                data = _json.loads(_s[_a : _b + 1])
            except Exception:
                data = None
        if data is None:
            new_count = consecutive_malformed + 1
            _log.warning(
                "llm_response_malformed reason=json_parse_error count=%d raw=%r exc=%s",
                new_count,
                raw_json[:500],
                exc,
            )
            return ValidationResult(
                valid=False,
                malformed_count=new_count,
                switch_to_fallback=new_count >= _MALFORMED_STRIKE_LIMIT,
                disable_gate=fallback_already_failed,
                reason=f"JSON parse error: {exc}",
            )

    confidence = data.get("confidence")
    if not isinstance(confidence, int | float) or not (0.0 <= float(confidence) <= 1.0):
        new_count = consecutive_malformed + 1
        _log.warning(
            "llm_response_malformed reason=invalid_confidence count=%d confidence=%r raw=%r",
            new_count,
            confidence,
            raw_json[:500],
        )
        return ValidationResult(
            valid=False,
            malformed_count=new_count,
            switch_to_fallback=new_count >= _MALFORMED_STRIKE_LIMIT,
            disable_gate=fallback_already_failed,
            reason=f"confidence invalid: {confidence!r} (must be float 0.0-1.0)",
        )

    reasoning = data.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        new_count = consecutive_malformed + 1
        _log.warning(
            "llm_response_malformed reason=empty_reasoning count=%d raw=%r",
            new_count,
            raw_json[:500],
        )
        return ValidationResult(
            valid=False,
            malformed_count=new_count,
            switch_to_fallback=new_count >= _MALFORMED_STRIKE_LIMIT,
            disable_gate=fallback_already_failed,
            reason=f"reasoning invalid: {reasoning!r} (must be non-empty string)",
        )

    return ValidationResult(
        valid=True,
        malformed_count=0,  # reset on success
        switch_to_fallback=False,
        disable_gate=False,
        reason="OK",
    )


def should_resume_llm(state: LLMRecoveryState) -> bool:
    """Return True when enough DSL-only trades have passed to re-enable the LLM gate."""
    return state.dsl_only_trades >= _DSL_ONLY_RECOVERY_TRADES


def _win_rate(outcomes: list[SignalOutcome], approved: bool) -> tuple[float, int]:
    """Returns (win_rate, count) for outcomes filtered by approved flag."""
    bucket = [o for o in outcomes if o.was_approved == approved]
    if not bucket:
        return 0.0, 0
    wins = sum(1 for o in bucket if o.would_have_won)
    return wins / len(bucket), len(bucket)


def evaluate_llm_sanity(
    recent_outcomes: list[SignalOutcome],
    rolling_window: int = _ROLLING_WINDOW,
) -> SanityReport:
    """
    Evaluate whether the LLM confirmation gate is adding alpha or destroying it.

    Args:
        recent_outcomes: Most-recent signal outcomes (both approved and rejected),
                         oldest first.  Caller truncates to keep history bounded.
        rolling_window: How many of the most recent outcomes to consider.

    Returns:
        SanityReport with verdict and bypass recommendation.
    """
    window = (
        recent_outcomes[-rolling_window:]
        if len(recent_outcomes) > rolling_window
        else recent_outcomes
    )

    approved_wr, approved_n = _win_rate(window, approved=True)
    rejected_wr, rejected_n = _win_rate(window, approved=False)

    if approved_n < _MIN_SAMPLES_EACH or rejected_n < _MIN_SAMPLES_EACH:
        return SanityReport(
            verdict=WatchdogVerdict.INSUFFICIENT_DATA,
            approved_win_rate=round(approved_wr, 4),
            rejected_win_rate=round(rejected_wr, 4),
            approved_sample_count=approved_n,
            rejected_sample_count=rejected_n,
            reason=(
                f"Need >= {_MIN_SAMPLES_EACH} samples in each bucket "
                f"(approved={approved_n}, rejected={rejected_n})"
            ),
            bypass_llm=False,
        )

    if rejected_wr > approved_wr:
        verdict = WatchdogVerdict.LLM_HURTING_ALPHA
        reason = (
            f"Rejected-signal WR {rejected_wr:.1%} > approved WR {approved_wr:.1%}"
            f" over last {len(window)} outcomes -- LLM is filtering out good signals."
            " Bypass LLM gate."
        )
        bypass = True
    else:
        verdict = WatchdogVerdict.LLM_ADDING_VALUE
        reason = (
            f"Approved WR {approved_wr:.1%} >= rejected WR {rejected_wr:.1%}"
            " -- LLM is adding value. Keep gate active."
        )
        bypass = False

    return SanityReport(
        verdict=verdict,
        approved_win_rate=round(approved_wr, 4),
        rejected_win_rate=round(rejected_wr, 4),
        approved_sample_count=approved_n,
        rejected_sample_count=rejected_n,
        reason=reason,
        bypass_llm=bypass,
    )
