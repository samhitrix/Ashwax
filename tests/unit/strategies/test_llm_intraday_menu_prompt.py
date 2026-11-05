"""Menu selection + short decision prompt for llm_intraday_agent (2026-09-28).

Before: the LLM was shown the top-25 "most active" stocks by |chg%| x volume-ratio with NO liquidity
filter and no memory (thin stocks like BIKAJI/SAREGAMA/SIGNATURE filled the menu again and again; 41%
of each pass repeated the previous one), inside a ~7.5k-character JSON blob, with a long instruction
block. Now: liquid, not-recently-offered stocks only, a compact table, and one clear question.
"""

from __future__ import annotations

import json

from src.strategies.llm_intraday.pipeline import (
    ValidationLimits,
    build_market_context,
    build_prompt,
    build_system_prompt,
    parse_llm_response,
    select_menu,
)


def _f(chg: float, volx: float, ltp: float = 500.0) -> dict:
    return {
        "ltp": ltp,
        "chg_pct": chg,
        "vol_ratio20": volx,
        "gap_pct": -1.0,
        "vwap_dist_pct": -0.8,
        "or_position": "below",
        "off_day_low_pct": 0.9,
        "off_day_high_pct": 3.1,
        "atr14_pct": 0.25,
        "rs_vs_nifty_pct": -1.4,
        "day_high": 515.0,
        "day_low": 495.0,
    }


FEATS = {
    "BIGBANK": _f(-3.0, 3.0),  # score 9.0, liquid
    "THINCO": _f(-4.0, 9.0),  # score 36.0 but illiquid
    "MIDCAP": _f(2.0, 2.0),  # score 4.0, liquid
    "CHEAP": _f(-6.0, 5.0, ltp=40.0),  # under price floor
    "NOADV": _f(-5.0, 5.0),  # no ADV data
}
ADV = {"BIGBANK": 600.0, "THINCO": 19.0, "MIDCAP": 120.0, "CHEAP": 900.0}


def _menu(**kw) -> list[str]:
    args = dict(
        n=10,
        min_price=100.0,
        adv_cr=ADV,
        min_adv_cr=50.0,
        offered={},
        now_ts=10_000.0,
        re_offer_s=3600.0,
    )
    args.update(kw)
    return select_menu(FEATS, **args)


def test_illiquid_unknown_and_cheap_stocks_never_reach_the_llm() -> None:
    m = _menu()
    assert m == ["BIGBANK", "MIDCAP"]  # THINCO (19cr), NOADV (no data), CHEAP (price) all excluded


def test_liquidity_floor_is_configurable_and_zero_disables_it() -> None:
    assert "THINCO" in _menu(min_adv_cr=10.0)
    assert "THINCO" in _menu(min_adv_cr=0.0)


def test_recently_offered_stocks_are_not_shown_again() -> None:
    assert _menu(offered={"BIGBANK": 10_000.0 - 600}) == ["MIDCAP"]  # offered 10 min ago
    assert "BIGBANK" in _menu(offered={"BIGBANK": 10_000.0 - 7200})  # 2h ago: eligible again


def test_menu_is_ranked_by_activity_and_capped() -> None:
    assert _menu(n=1) == ["BIGBANK"]


def test_nothing_new_to_show_gives_an_empty_menu_not_a_padded_one() -> None:
    assert _menu(offered={"BIGBANK": 9_999.0, "MIDCAP": 9_999.0}) == []


# ── prompt ───────────────────────────────────────────────────────────────────


def _ctx(n: int = 10):
    feats = {f"S{i}": _f(-3.0 - i * 0.1, 4.0) for i in range(n)}
    return build_market_context(
        nifty_chg_pct=-1.2,
        intraday_regime="BEAR",
        daily_regime="STRONG_BEAR",
        bearish_pct=0.85,
        vix=12.3,
        sector_status={"PHARMA": "LEADING"},
        features=feats,
        universe=list(feats),
        news={},
        minutes_to_flat=90,
    )


def test_prompt_is_a_compact_table_not_a_json_blob() -> None:
    p = build_prompt(_ctx(), [], [])
    assert '"candidate_universe"' not in p
    assert "S0" in p and "S9" in p
    lines = [ln for ln in p.splitlines() if ln.startswith("S")]
    assert len(lines) == 10  # one short line per stock
    assert all(len(ln) < 130 for ln in lines)


def test_prompt_is_a_fraction_of_the_old_size() -> None:
    p = build_prompt(_ctx(10), [], [])
    old = len(json.dumps(_ctx(25), separators=(",", ":")))  # what 25 stocks used to cost
    assert len(p) < 3500 and len(p) < old / 2


def test_prompt_tells_the_model_how_much_range_is_left_for_each_stock() -> None:
    p = build_prompt(_ctx(), [], [])
    # atr14_pct 0.25 x sqrt(90/5) = 1.06 -> "range_left" column
    assert "range_left" in p and "1.06" in p


def test_system_prompt_asks_one_clear_question_and_defaults_to_pass() -> None:
    s = build_system_prompt(ValidationLimits())
    assert len(s) < 2600
    assert "PASS" in s and "decision" in s
    assert "chasing" in s.lower() or "room" in s.lower()


def test_a_reply_with_a_decision_field_is_still_parsed() -> None:
    feats = {"AXIS": {"ltp": 1000.0, "atr14_pct": 0.2, "chg_pct": -2.0, "vol_ratio20": 3.0}}
    rejected: list = []
    reply = json.dumps(
        {
            "decision": "TRADE",
            "thesis": "weak tape",
            "ideas": [
                {
                    "symbol": "AXIS",
                    "direction": "SHORT",
                    "entry_price": 1000.0,
                    "stop_loss": 1007.0,
                    "target": 987.0,
                    "confidence": 0.6,
                    "reasoning": "x",
                }
            ],
        }
    )
    thesis, ideas = parse_llm_response(reply, feats, ValidationLimits(), 90, rejected)
    assert thesis == "weak tape" and len(ideas) == 1 and ideas[0].symbol == "AXIS"


def test_a_pass_reply_yields_no_ideas() -> None:
    feats = {"AXIS": {"ltp": 1000.0, "atr14_pct": 0.2}}
    rejected: list = []
    thesis, ideas = parse_llm_response(
        json.dumps({"decision": "PASS", "thesis": "nothing clean", "ideas": []}),
        feats,
        ValidationLimits(),
        90,
        rejected,
    )
    assert ideas == [] and thesis == "nothing clean"
