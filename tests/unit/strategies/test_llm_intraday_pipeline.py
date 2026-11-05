"""Tests for src/strategies/llm_intraday/pipeline.py — the pure feature /
validation layer of the LLM intraday agent. The validation rules are the
safety-critical part: an LLM can hallucinate a price or propose a stop inside
intraday noise, and nothing downstream re-checks the idea against the market."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from src.strategies.llm_intraday.pipeline import (
    ValidationLimits,
    build_market_context,
    build_prompt,
    build_symbol_features,
    parse_llm_response,
    run_intraday_reasoning,
    select_in_play,
    still_valid_at,
)

_IST = ZoneInfo("Asia/Kolkata")
_NOW = datetime(2026, 9, 11, 10, 45, tzinfo=_IST)
_LIMITS = ValidationLimits()


def _df(prev_close: float = 100.0, today_closes: list[float] | None = None) -> pd.DataFrame:
    """Two sessions of 5m bars: 30 flat bars yesterday, then today's bars."""
    today_closes = today_closes or [101.0, 101.5, 102.0, 102.5, 103.0, 103.5]
    y_idx = pd.date_range("2026-09-10 12:45", periods=30, freq="5min", tz=_IST)
    t_idx = pd.date_range("2026-09-11 09:15", periods=len(today_closes), freq="5min", tz=_IST)
    rows = [(prev_close, prev_close + 0.2, prev_close - 0.2, prev_close)] * 30
    for c in today_closes:
        rows.append((c - 0.3, c + 0.2, c - 0.4, c))
    o, h, lo, c = zip(*rows, strict=True)
    return pd.DataFrame(
        {"open": o, "high": h, "low": lo, "close": c, "volume": [10_000.0] * len(rows)},
        index=y_idx.append(t_idx),
    )


_FEATS = {
    "RELIANCE": {"ltp": 1000.0, "atr14_pct": 0.2, "chg_pct": 1.5, "vol_ratio20": 2.0},
    "INFY": {"ltp": 1500.0, "atr14_pct": 0.5, "chg_pct": -1.0, "vol_ratio20": 1.5},
}


def _raw(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "symbol": "RELIANCE",
        "direction": "LONG",
        "entry_price": 1000.0,
        "stop_loss": 993.0,  # 0.7% stop
        "target": 1012.0,  # 1.2% target, RR 1.71
        "confidence": 0.7,
        "reasoning": "held VWAP",
    }
    base.update(kw)
    return base


def _parse(*ideas: dict[str, object]) -> list[object]:
    _, out = parse_llm_response(json.dumps({"thesis": "t", "ideas": list(ideas)}), _FEATS, _LIMITS)
    return out


# ── features ────────────────────────────────────────────────────────────────


def test_features_use_prior_session_close_and_today_only_vwap() -> None:
    f = build_symbol_features(_df(), _NOW)
    assert f is not None
    assert f["ltp"] == 103.5
    assert f["chg_pct"] == pytest.approx(3.5)
    assert f["gap_pct"] == pytest.approx(0.7)  # today's first open 100.7 vs 100 close
    # OR = 09:15-09:30 bars (first three): high 102.2, low 100.6 → ltp above.
    assert f["or_high"] == pytest.approx(102.2)
    assert f["or_low"] == pytest.approx(100.6)
    assert f["or_position"] == "above"
    # VWAP over today's bars only sits well above yesterday's flat 100 prints.
    assert f["vwap_dist_pct"] is not None and 0 < f["vwap_dist_pct"] < 2.0


def test_features_none_without_prior_session_or_enough_bars() -> None:
    df = _df()
    today_only = df[df.index.tz_convert(_IST).date == _NOW.date()]
    assert build_symbol_features(today_only, _NOW) is None
    assert build_symbol_features(_df(today_closes=[101.0, 101.2]), _NOW) is None


def test_select_in_play_ranks_by_move_times_volume_and_drops_cheap() -> None:
    feats = {
        "A": {"ltp": 500.0, "chg_pct": 1.0, "vol_ratio20": 1.0},
        "B": {"ltp": 500.0, "chg_pct": -2.0, "vol_ratio20": 2.0},
        "C": {"ltp": 50.0, "chg_pct": 9.0, "vol_ratio20": 9.0},  # below min price
        "D": {"ltp": 500.0, "chg_pct": 1.5, "vol_ratio20": None},
    }
    assert select_in_play(feats, 2) == ["B", "D"]


def test_market_context_and_prompt_include_universe_and_memory() -> None:
    ctx = build_market_context(
        nifty_chg_pct=-0.4,
        intraday_regime="BEAR",
        daily_regime="BULL",
        bearish_pct=0.62,
        vix=14.0,
        sector_status={"IT": "LEADING", "BANK": "DECLINING"},
        features=_FEATS,
        universe=["INFY", "MISSING"],
        news={"INFY": "Q2 beat", "RELIANCE": "not in universe"},
    )
    assert [c["symbol"] for c in ctx["candidate_universe"]] == ["INFY"]
    assert ctx["news"] == {"INFY": "Q2 beat"}
    assert ctx["breadth_bearish_pct"] == 62.0
    outcomes = [
        {
            "entry_date": "2026-09-10",
            "symbol": "TCS",
            "direction": "long",
            "r": 1.2,
            "exit_reason": "Target",
        },
        {
            "entry_date": "2026-09-10",
            "symbol": "SBIN",
            "direction": "short",
            "r": -1.0,
            "exit_reason": "SL Hit",
        },
    ]
    prompt = build_prompt(ctx, outcomes, [])
    assert "wins=1/2" in prompt and "TCS LONG +1.20R" in prompt
    assert "(first pass today)" in prompt


# ── validation ──────────────────────────────────────────────────────────────


def test_accepts_valid_long_and_short() -> None:
    # 1.1% stop (≥ 2×0.5 ATR floor), 1.8% target → RR 1.64.
    short = _raw(
        symbol="INFY", direction="SHORT", entry_price=1500.0, stop_loss=1516.5, target=1473.0
    )
    ideas = _parse(_raw(), short)
    assert [(i.symbol, i.direction) for i in ideas] == [("RELIANCE", "LONG"), ("INFY", "SHORT")]


def test_buy_sell_aliases_normalised() -> None:
    (idea,) = _parse(_raw(direction="buy"))
    assert idea.direction == "LONG"


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": "TCS"},  # not in the offered universe
        {"direction": "SIDEWAYS"},
        {"entry_price": 1010.0, "stop_loss": 1003.0, "target": 1022.0},  # 1% drift
        {"stop_loss": 1005.0},  # long stop above entry
        {"stop_loss": 996.0},  # 0.4% stop < 0.5% floor
        {"stop_loss": 970.0, "target": 1080.0},  # 3% stop > 2.5% max
        {"stop_loss": 994.0, "target": 1005.0},  # 0.5% target < 0.6% min
        {"stop_loss": 990.0, "target": 1012.0},  # RR 1.2 < 1.5
    ],
)
def test_rejects_bad_ideas(overrides: dict[str, object]) -> None:
    assert _parse(_raw(**overrides)) == []


def test_reward_risk_compares_at_display_precision() -> None:
    """Journal row 2026-09-23 (VTL): raw ratio 1.0479/0.7 = 1.49714..., which rounds
    to display as 1.50 — exactly the 1.5 floor — yet the idea was rejected because the
    gate compared the unrounded float. Prices are already quantized to paise, so
    comparing at full float precision manufactures a phantom miss that isn't real at
    the instrument's actual tick granularity. The gate must compare at the same
    precision it reports, so a rounded tie with the floor now passes."""
    tie = _raw(entry_price=1000.0, stop_loss=993.0, target=1010.48)
    assert len(_parse(tie)) == 1


def test_stop_floor_scales_with_atr() -> None:
    # INFY atr14_pct 0.5 → floor = max(0.5, 2.0×0.5) = 1.0%; a 0.8% stop is noise.
    tight = _raw(symbol="INFY", entry_price=1500.0, stop_loss=1488.0, target=1530.0)
    assert _parse(tight) == []
    ok = _raw(symbol="INFY", entry_price=1500.0, stop_loss=1482.0, target=1530.0)
    assert len(_parse(ok)) == 1


def test_short_levels_must_be_inverted() -> None:
    assert _parse(_raw(direction="SHORT")) == []  # long-shaped levels on a short


def test_malformed_and_fenced_responses() -> None:
    assert parse_llm_response("not json", _FEATS, _LIMITS) == (None, [])
    assert parse_llm_response("[1,2]", _FEATS, _LIMITS) == (None, [])
    fenced = "```json\n" + json.dumps({"thesis": "x", "ideas": [_raw()]}) + "\n```"
    thesis, ideas = parse_llm_response(fenced, _FEATS, _LIMITS)
    assert thesis == "x" and len(ideas) == 1


def test_still_valid_at_shifts_levels_with_price_preserving_rr() -> None:
    """2026-09-21 fix: still_valid_at shifts stop/target by the original
    distance from the new price instead of re-checking frozen absolute
    levels, so R:R survives small drift in either direction (the frozen-level
    version rejected both a 0.4% pullback and a 0.4% rally here, for the same
    underlying reason: whichever side price approaches, the fixed level's
    remaining distance shrinks — the fix was letting a live POONAWALLA idea
    drift 0.25% toward its own target collapse its R:R from 1.70 to 1.15 and
    get rejected on 2026-09-18, even though nothing about the setup changed)."""
    idea = {**_raw(), "atr14_pct": 0.2}  # entry 1000, stop 993 (0.7%), target 1012, RR 1.71
    assert still_valid_at(idea, 1000.0, _LIMITS)
    # Small pullback (-0.4%): stop distance shifts to 989, still 0.7% of the
    # new price and RR unchanged — no longer punished for noise-level drift.
    assert still_valid_at(idea, 996.0, _LIMITS)
    # Small rally (+0.4%, the POONAWALLA case): target shifts to 1016, RR
    # stays 1.71 instead of collapsing to 0.73 — this is the actual fix.
    assert still_valid_at(idea, 1004.0, _LIMITS)
    # A large rally still fails on its own terms: the shifted 7pt stop is
    # only 0.48% of 1450 — below the 0.5% floor — confirming the shift
    # doesn't disable the stop-floor/ceiling checks, only removes their
    # sensitivity to noise-level drift.
    assert not still_valid_at(idea, 1450.0, _LIMITS)


def test_limits_from_cfg_ignores_unknown_keys() -> None:
    lim = ValidationLimits.from_cfg(
        {"min_stop_pct": 0.8, "enabled": True, "decision_times_ist": []}
    )
    assert lim.min_stop_pct == 0.8
    assert lim.max_stop_pct == ValidationLimits().max_stop_pct


# ── run pass ────────────────────────────────────────────────────────────────


class _Client:
    _provider = "gemini"

    def __init__(self, text: str | None = None, exc: Exception | None = None) -> None:
        self._text, self._exc = text, exc

    async def complete(self, prompt: str, **_: object) -> str:
        if self._exc:
            raise self._exc
        return self._text or ""


@pytest.mark.asyncio  # type: ignore[misc]
async def test_run_caps_ideas_and_never_raises() -> None:
    text = json.dumps(
        {
            "thesis": "t",
            "ideas": [
                _raw(),
                _raw(symbol="INFY", entry_price=1500.0, stop_loss=1482.0, target=1530.0),
            ],
        }
    )
    res = await run_intraday_reasoning(_Client(text), {}, [], [], _FEATS, _LIMITS, max_ideas=1)
    assert res.decision == "proposed" and len(res.ideas) == 1
    assert res.llm_model_used == "gemini"

    failed = await run_intraday_reasoning(
        _Client(exc=RuntimeError("quota")), {}, [], [], _FEATS, _LIMITS, 2
    )
    # A provider quota error is OUR failure, not the agent judging the tape and
    # standing aside. It used to be recorded as "no_ideas" and replayed to the model
    # that way on the next pass, so a dead provider looked like a quiet market.
    assert failed.decision == "llm_failed" and failed.ideas == []
    assert failed.raw_llm_response, "the failure must leave evidence, not an empty row"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_ideas_killed_by_validation_are_not_recorded_as_no_ideas() -> None:
    """Journal row id=25 (2026-09-22): the model proposed ATGL and CONCOR, both died
    on reward:risk, and the pass was filed as `no_ideas`. todays_runs() feeds that
    straight back into the next prompt, so the agent was told it had declined to
    trade on a pass where it had in fact proposed two trades we threw away -- it
    could not learn its targets were too small, and kept proposing the same shape."""
    text = json.dumps(
        {
            "thesis": "t",
            # entry at the live LTP, 1.0% stop, 1.0% target -> R:R 1.0, under the 1.5
            # floor. Everything else about the idea is valid, so R:R is what kills it.
            "ideas": [_raw(entry_price=1000.0, stop_loss=990.0, target=1010.0)],
        }
    )
    res = await run_intraday_reasoning(_Client(text), {}, [], [], _FEATS, _LIMITS, max_ideas=2)
    assert res.ideas == []
    assert res.decision == "rejected_by_validation"
    assert [r["reason"] for r in res.rejected_ideas] == ["reward_risk_too_low"]
    # The numbers must travel with the reason, or the model cannot tell by how much
    # it missed and simply re-proposes the same trade.
    assert res.rejected_ideas[0]["reward_risk"] < _LIMITS.min_reward_risk


@pytest.mark.asyncio  # type: ignore[misc]
async def test_model_genuinely_standing_aside_is_still_no_ideas() -> None:
    """The new label must not swallow the real one: an empty ideas list is a decision."""
    res = await run_intraday_reasoning(
        _Client(json.dumps({"thesis": "chop, nothing clean", "ideas": []})),
        {},
        [],
        [],
        _FEATS,
        _LIMITS,
        max_ideas=2,
    )
    assert res.decision == "no_ideas" and res.rejected_ideas == []


class _SeqClient:
    """Replies in order; an Exception item is raised instead of returned."""

    _provider = "gemini"

    def __init__(self, *replies: object) -> None:
        self._replies = list(replies)
        self.calls = 0

    async def complete(self, prompt: str, **_: object) -> str:
        self.calls += 1
        r = self._replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return str(r)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_empty_reply_is_retried_not_recorded_as_no_ideas() -> None:
    # 2026-09-21 13:45: a provider outage produced an empty reply that was logged as
    # a clean no_ideas pass and cost the day's last slot.
    good = json.dumps({"thesis": "t", "ideas": [_raw()]})
    client = _SeqClient("", good)
    res = await run_intraday_reasoning(client, {}, [], [], _FEATS, _LIMITS, max_ideas=2)
    assert client.calls == 2
    assert res.decision == "proposed" and len(res.ideas) == 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_call_error_is_retried_once_then_gives_up() -> None:
    good = json.dumps({"thesis": "t", "ideas": [_raw()]})
    ok = _SeqClient(RuntimeError("503"), good)
    res = await run_intraday_reasoning(ok, {}, [], [], _FEATS, _LIMITS, max_ideas=2)
    assert ok.calls == 2 and res.decision == "proposed"

    dead = _SeqClient(RuntimeError("503"), RuntimeError("503"))
    res2 = await run_intraday_reasoning(dead, {}, [], [], _FEATS, _LIMITS, max_ideas=2)
    assert dead.calls == 2 and res2.ideas == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_genuine_no_setup_reply_is_accepted_first_time() -> None:
    client = _SeqClient(json.dumps({"thesis": "nothing clean", "ideas": []}))
    res = await run_intraday_reasoning(client, {}, [], [], _FEATS, _LIMITS, max_ideas=2)
    assert client.calls == 1 and res.decision == "no_ideas"


# ── target reachability ─────────────────────────────────────────────────────


def _parse_at(minutes_left: float, limits: ValidationLimits) -> list[object]:
    raw = json.dumps({"thesis": "t", "ideas": [_raw()]})  # 1.2% target, 5m ATR 0.2%
    return parse_llm_response(raw, _FEATS, limits, minutes_left)[1]


def test_reachability_scales_with_sqrt_time_not_a_clock_cutoff() -> None:
    on = ValidationLimits(reach_mult=1.0)
    assert _parse_at(340, on)  # 09:30: reach 0.2*sqrt(68)=1.65% >= 1.2%
    assert not _parse_at(97, on)  # 13:33: reach 0.2*sqrt(19.4)=0.88% < 1.2%


def test_reachability_off_by_default_and_without_clock() -> None:
    assert _parse_at(10, _LIMITS)  # reach_mult 0 → never checked
    assert parse_llm_response(
        json.dumps({"ideas": [_raw()]}), _FEATS, ValidationLimits(reach_mult=1.0)
    )[1]  # no minutes_left → not checked


async def _reasoning(idea: dict[str, object], limits: ValidationLimits) -> object:
    text = json.dumps({"thesis": "t", "ideas": [idea]})
    return await run_intraday_reasoning(_Client(text), {}, [], [], _FEATS, limits, max_ideas=2)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_stop_that_displays_as_the_floor_is_not_rejected_as_too_tight() -> None:
    """Journal 2026-09-23..30: ideas rejected `stop_too_tight` with stop_pct == floor_pct
    as logged (1.57 vs 1.57, 0.5 vs 0.5). Raw 0.496% displays as the 0.50% floor."""
    ok = await _reasoning(_raw(stop_loss=995.04, target=1010.0), _LIMITS)
    assert [i.symbol for i in ok.ideas] == ["RELIANCE"]  # type: ignore[attr-defined]
    tight = await _reasoning(_raw(stop_loss=995.5, target=1010.0), _LIMITS)  # 0.45%
    assert [r["reason"] for r in tight.rejected_ideas] == ["stop_too_tight"]  # type: ignore[attr-defined]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_allowed_directions_filters_longs_and_defaults_to_both() -> None:
    short_only = ValidationLimits.from_cfg({"allowed_directions": ["short"]})
    assert short_only.allowed_directions == ("SHORT",)
    long_idea = _raw()
    short_idea = _raw(direction="SHORT", entry_price=1000.0, stop_loss=1007.0, target=988.0)
    res = await _reasoning(long_idea, short_only)
    assert res.ideas == []  # type: ignore[attr-defined]
    assert [r["reason"] for r in res.rejected_ideas] == ["direction_not_allowed"]  # type: ignore[attr-defined]
    res = await _reasoning(short_idea, short_only)
    assert [i.direction for i in res.ideas] == ["SHORT"]  # type: ignore[attr-defined]
    # key absent -> both directions, behaviour unchanged
    both = ValidationLimits.from_cfg({})
    assert (await _reasoning(long_idea, both)).ideas  # type: ignore[attr-defined]
