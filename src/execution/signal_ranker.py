"""Rank competing intraday breakout signals within one scan cycle.

The intraday scan can surface several breakouts in the same cycle but places at
most one order. Historically it took whichever symbol sorted first, which is
arbitrary — this module scores candidates so the *best* one can be chosen
instead.

Live-wired, not shadow-only: `orchestrator.py` uses `score_candidate`/`rank_candidates`
to place real intraday orders and `select_top_candidates` (via `_score_swing_candidate`/
`_select_top_candidates`) to pick real swing (real-money) entries. Any change to the
scoring here is a change to real-money-affecting logic and must go through shadow
validation before being flipped live (see `config/learning.json` → `signal_ranker.mode`).

Pure functions, no I/O — every input is passed in explicitly so this is testable
without a DB, broker, or live feed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)  # nests under "src.execution" -> execution.log

# Component weights. Volume and follow-through carry the most weight: both are
# measured on the breakout bar itself and are the classic false-breakout tells.
# Width and index alignment are context, not confirmation, so they weigh less.
W_VOLUME = 0.30
W_FOLLOW_THROUGH = 0.30
W_WIDTH = 0.20
W_ALIGNMENT = 0.20

# Opening-range width band (% of price). Below MIN the range is noise with no
# momentum to break out of; above MAX the implied stop is too wide to price a
# sane R:R. Mirrors the compiler's existing `opening_range_width` gate defaults.
WIDTH_MIN_PCT = 0.30
WIDTH_MAX_PCT = 1.50


@dataclass(frozen=True)
class CandidateScore:
    """Score plus its components, so a ranking decision is explainable in logs."""

    score: float
    components: dict[str, float] = field(default_factory=dict)


def _clamp01(x: float) -> float:
    if x != x:  # NaN
        return 0.0
    return max(0.0, min(1.0, x))


def _volume_score(breakout_volume: float | None, avg_volume: float | None) -> float:
    """Breakout-bar volume vs its own recent average, saturating at 2x."""
    if not breakout_volume or not avg_volume or avg_volume <= 0:
        return 0.5  # unknown — neutral, never rewarded or punished
    return _clamp01((breakout_volume / avg_volume) / 2.0)


def _width_score(or_width_pct: float | None) -> float:
    """Peaks mid-band, falls to 0 outside [WIDTH_MIN_PCT, WIDTH_MAX_PCT]."""
    if or_width_pct is None or or_width_pct != or_width_pct or or_width_pct <= 0:
        return 0.5
    if or_width_pct <= WIDTH_MIN_PCT or or_width_pct >= WIDTH_MAX_PCT:
        return 0.0
    mid = (WIDTH_MIN_PCT + WIDTH_MAX_PCT) / 2.0
    half = (WIDTH_MAX_PCT - WIDTH_MIN_PCT) / 2.0
    return _clamp01(1.0 - abs(or_width_pct - mid) / half)


def _follow_through_score(
    bar_high: float, bar_low: float, bar_close: float, is_short: bool
) -> float:
    """Where the breakout bar closed inside its own range.

    A long closing at the high (or a short at the low) shows the move was
    carried into the close; one closing mid-bar was already being faded.
    """
    rng = bar_high - bar_low
    if rng <= 0 or rng != rng:
        return 0.5
    pos = (bar_close - bar_low) / rng
    return _clamp01(1.0 - pos if is_short else pos)


def _alignment_score(nifty_aligned: bool | None) -> float:
    """1.0 with the index, 0.0 against it, 0.5 when the index read is missing.

    Fail-neutral on None matches the fail-open stance the regime gates use: a
    missing benchmark must not silently rank a candidate last.
    """
    if nifty_aligned is None:
        return 0.5
    return 1.0 if nifty_aligned else 0.0


def score_candidate(
    *,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    is_short: bool,
    breakout_volume: float | None = None,
    avg_volume: float | None = None,
    or_width_pct: float | None = None,
    nifty_aligned: bool | None = None,
) -> CandidateScore:
    """Score one breakout candidate in [0, 1]. Higher is better.

    Every component degrades to a neutral 0.5 when its input is unavailable, so
    a candidate is never ranked last merely because data was missing.
    """
    vol = _volume_score(breakout_volume, avg_volume)
    ft = _follow_through_score(bar_high, bar_low, bar_close, is_short)
    width = _width_score(or_width_pct)
    align = _alignment_score(nifty_aligned)
    total = vol * W_VOLUME + ft * W_FOLLOW_THROUGH + width * W_WIDTH + align * W_ALIGNMENT
    return CandidateScore(
        score=round(total, 4),
        components={
            "volume": round(vol, 3),
            "follow_through": round(ft, 3),
            "width": round(width, 3),
            "alignment": round(align, 3),
        },
    )


def rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort scored candidates best-first.

    Ties break on symbol so the ordering is deterministic and reproducible
    across runs rather than depending on dict insertion order.
    """
    return sorted(
        candidates,
        key=lambda c: (-float(c.get("score", 0.0)), str(c.get("symbol", ""))),
    )


def rank_candidates_by_sqs(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort scored candidates best-first by Signal Quality Score (spec §10,
    src/intelligence/signal_quality_score.py) instead of the legacy 4-weight
    score — used only when config/learning.json signal_ranker.mode_{segment} is
    "live" (see orchestrator.py's _signal_ranker_mode). A candidate missing an
    "sqs" entry (adapter failure, or built before the mode was flipped live)
    falls back to its legacy score, scaled onto SQS's 0-100 range, rather than
    being silently dropped to the bottom of the pool.

    A candidate carrying "priority_weight" (Layer 4A Strategy Selector Adapter,
    config/learning.json strategy_selector.mode=="live" only — callers omit
    this key entirely in "off"/"shadow" mode) gets it folded in as a bounded
    multiplicative tie-break: final = sqs_total * (0.5 + 0.5*weight), so a
    strategy currently out of favor in this regime is never ranked to zero
    just for that, matching this module's existing "never rank last merely
    because data was missing" philosophy.
    """

    def _key(c: dict[str, Any]) -> tuple[float, str]:
        sqs = c.get("sqs")
        total = float(sqs["total"]) if sqs else float(c.get("score", 0.0)) * 100.0
        weight = c.get("priority_weight")
        if weight is not None:
            total *= 0.5 + 0.5 * float(weight)
        return (-total, str(c.get("symbol", "")))

    return sorted(candidates, key=_key)


def select_top_candidates(
    candidates: list[dict[str, Any]],
    *,
    max_total: int,
    max_per_strategy: int,
    per_strategy_counts: dict[str, int] | None = None,
    use_sqs: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick the best candidates across ALL strategies, respecting both caps.

    Returns (accepted, rejected) with rejected preserving rank order.

    The swing scan used to rank inside its per-strategy loop, so the first strategy
    evaluated consumed the whole daily cap and a genuinely better setup from a later
    strategy never got a look. Ranking has to happen over the combined pool for
    "best N of the day" to mean anything.

    `per_strategy_counts` carries trades already placed today (DB-sourced), so a
    strategy at its own cap is skipped without consuming a daily slot.

    One symbol never gets two accepted candidates in the same call, even from two
    different strategies — the best-ranked one wins, the rest are rejected here,
    structurally, regardless of fill timing (found 2026-09-07: CGCL got two
    same-day CNC orders, minervini_vcp and oneil_canslim, because the only other
    same-symbol guard checks order_monitor's FILLED positions — an AMO still
    resting from an earlier candidate in this same batch doesn't show up there
    yet, so it couldn't have caught this; this cap has to hold before either
    candidate ever reaches execution).
    """
    _log.debug(
        "select_top_candidates_received n=%d max_total=%d max_per_strategy=%d use_sqs=%s",
        len(candidates),
        max_total,
        max_per_strategy,
        use_sqs,
    )
    counts = dict(per_strategy_counts or {})
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    accepted_symbols: set[str] = set()
    _ranked = rank_candidates_by_sqs(candidates) if use_sqs else rank_candidates(candidates)
    for cand in _ranked:
        strategy = str(cand.get("strategy_name", ""))
        symbol = str(cand.get("symbol", ""))
        if symbol in accepted_symbols:
            _log.debug(
                "select_top_candidates_found symbol=%s strategy=%s action=rejected "
                "reason=symbol_already_accepted",
                symbol,
                strategy,
            )
            rejected.append(cand)
            continue
        if len(accepted) >= max_total or counts.get(strategy, 0) >= max_per_strategy:
            _log.debug(
                "select_top_candidates_found symbol=%s strategy=%s action=rejected "
                "reason=ranked_below_cap",
                symbol,
                strategy,
            )
            rejected.append(cand)
            continue
        _log.debug(
            "select_top_candidates_found symbol=%s strategy=%s action=accepted",
            symbol,
            strategy,
        )
        accepted.append(cand)
        accepted_symbols.add(symbol)
        counts[strategy] = counts.get(strategy, 0) + 1
    return accepted, rejected
