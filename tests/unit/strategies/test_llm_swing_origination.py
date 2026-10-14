"""Regression guards for llm_swing origination.

Each test here corresponds to a defect that silently cost real trades, so the comments
name the incident rather than restating the assertion.
"""

from __future__ import annotations

import pandas as pd
import pytest
from src.strategies.llm_swing.pipeline import (
    SwingLimits,
    build_market_context,
    build_symbol_features,
    build_system_prompt,
    parse_llm_response,
)

_LAST_CLOSE = {"LAURUSLABS": 1901.7}


def test_fenced_json_reply_still_yields_the_candidate() -> None:
    """2026-09-16: gemini returned a valid LAURUSLABS idea (entry 1901.70 / sl 1825 /
    target 2055, R:R 2.0 -- through the 1.8 gate) wrapped in a ```json fence. The parser
    called json.loads on the raw string, threw, and the run was journalled as
    'no_trade_today'. The same name was bought five days later at 1973.80, ~3.7% worse.
    A parsing failure must never again be recorded as a decision to stand aside."""
    fenced = (
        "```json\n"
        '{"thesis": "Pharma relative strength.", "candidates": ['
        '{"symbol": "LAURUSLABS", "entry_price": 1901.7, "stop_loss": 1825.0, '
        '"target": 2055.0, "confidence": 0.55, "reasoning": "Pharma RS."}]}\n'
        "```"
    )
    thesis, candidates = parse_llm_response(fenced, _LAST_CLOSE)
    assert thesis is not None, "thesis was lost to the fence"
    assert len(candidates) == 1
    c = candidates[0]
    assert (c.target - c.entry_price) / (c.entry_price - c.stop_loss) >= 1.8


def test_prose_wrapped_reply_still_yields_the_candidate() -> None:
    """Same failure shape, different dressing: a model that prefixes a sentence before
    the JSON object. Extraction is by outermost braces, so this survives too."""
    noisy = (
        "Here is my analysis for today:\n"
        '{"thesis": "t", "candidates": [{"symbol": "LAURUSLABS", "entry_price": 1901.7, '
        '"stop_loss": 1825.0, "target": 2055.0, "confidence": 0.5, "reasoning": "r"}]}\n'
        "Let me know if you need more detail."
    )
    _, candidates = parse_llm_response(noisy, _LAST_CLOSE)
    assert len(candidates) == 1


def test_genuinely_malformed_reply_still_degrades_to_no_trade() -> None:
    """The unwrapping must not turn a real failure into a fake success — garbage still
    yields no candidates rather than a guess."""
    thesis, candidates = parse_llm_response("I cannot answer that.", _LAST_CLOSE)
    assert thesis is None and candidates == []


@pytest.mark.parametrize(
    ("field", "expected"),
    [("min_reward_risk", "1.80"), ("max_stop_loss_pct", "8.0")],
)
def test_prompt_states_the_gates_it_will_be_judged_against(field: str, expected: str) -> None:
    """The model proposed R:R 1.73 against a 1.80 floor and CRM G1 rejected it -- the
    bar was never in the prompt. If these numbers stop appearing, that silent failure
    mode is back."""
    prompt = build_system_prompt(SwingLimits(min_reward_risk=1.8, max_stop_loss_pct=8.0))
    assert expected in prompt, f"{field} missing from the origination prompt"


def test_prompt_states_spendable_cash_when_known() -> None:
    """CNC is fully funded -- 3 of 7 lifetime proposals died on cash. The model must be
    told what it can actually afford, and told nothing when the figure is unknown."""
    assert "29,311" in build_system_prompt(SwingLimits(spendable_cash=29311.0))
    assert "AFFORDABILITY" not in build_system_prompt(SwingLimits(spendable_cash=0.0))


def test_prompt_allows_cautious_trade_in_a_weak_regime() -> None:
    """38 of 45 passes returned no_trade_today, every thesis citing the bear regime.
    Policy: a leading sector with confirming relative strength is tradeable at reduced
    conviction, not an automatic reject -- while still not forcing trades."""
    prompt = build_system_prompt(SwingLimits())
    assert "REDUCED CONVICTION" in prompt
    assert "not by itself a reason to" in prompt.lower().replace("NOT", "not")
    assert "do not manufacture a trade" in prompt


def _frame(n: int = 250, start: float = 100.0) -> pd.DataFrame:
    """An uptrend that still has down-days. A monotonic ramp has zero losses, which makes
    RSI divide by zero and vanish — realistic-looking data that no real tape produces."""
    idx = pd.date_range("2025-01-01", periods=n, freq="D", tz="Asia/Kolkata")
    close = pd.Series(
        [start + i + (2.0 if i % 3 else -3.0) for i in range(n)], index=idx, dtype=float
    )
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": pd.Series([10_000] * n, index=idx, dtype=float),
        }
    )


def test_features_carry_relative_strength_and_technicals() -> None:
    """The universe used to be {symbol, last_close} only, so the model could not tell a
    leader from a laggard -- LAURUSLABS (beating NIFTY by 14.6% over a month) and
    ACUTAAS (below its own 50DMA) arrived looking identical."""
    stock, bench = _frame(), _frame(start=100.0)
    feats = build_symbol_features(
        "LAURUSLABS",
        stock,
        nifty_close=bench["close"],
        rs={"rs_percentile": 97.2, "rs_score": 0.41, "rs_momentum": 0.03, "rs_new_high": True},
        sector="PHARMA",
    )
    for key in ("rsi14", "atr14_pct", "pct_from_50dma", "pct_from_200dma"):
        assert key in feats, f"{key} missing -- the model cannot check technicals without it"
    for key in ("rs_excess_1m_pct", "rs_excess_3m_pct", "rs_percentile", "rs_excess_12m"):
        assert key in feats, f"{key} missing -- strength vs the index is the headline metric"
    assert feats["sector"] == "PHARMA"


def test_features_degrade_field_by_field_on_short_history() -> None:
    """A thin history must cost detail, not the candidate."""
    feats = build_symbol_features("X", _frame(n=5))
    assert feats["symbol"] == "X" and "last_close" in feats
    assert "pct_from_200dma" not in feats


def test_market_context_falls_back_when_features_absent() -> None:
    """Callers that pass no features keep the old shape rather than breaking."""
    ctx = build_market_context(
        regime="STRONG_BEAR",
        vix=11.0,
        rs_top20={"LAURUSLABS"},
        rs_blacklist=set(),
        sector_status={"PHARMA": "LEADING"},
        last_close=_LAST_CLOSE,
    )
    assert ctx["candidate_universe"] == [{"symbol": "LAURUSLABS", "last_close": 1901.7}]
    assert ctx["leading_sectors"] == ["PHARMA"]


def test_best_idea_is_not_discarded_for_being_listed_last() -> None:
    """The cap used to be applied as candidates[:3] in EMISSION order, so on a day the
    model produced more ideas than the cap, its strongest conviction could be thrown
    away purely for its position in the list — and nothing recorded that it existed."""
    from src.strategies.llm_swing.pipeline import SwingCandidate, select_best_candidates

    cands = [
        SwingCandidate("WEAK1", 100, 95, 110, 0.30, ""),
        SwingCandidate("WEAK2", 100, 95, 110, 0.35, ""),
        SwingCandidate("WEAK3", 100, 95, 110, 0.40, ""),
        SwingCandidate("BEST", 100, 95, 115, 0.85, ""),
    ]
    kept = [c.symbol for c in select_best_candidates(cands, 3)]
    assert kept[0] == "BEST", "highest-confidence idea must survive the cap"
    assert "WEAK1" not in kept


def test_ranking_is_deterministic_on_equal_confidence() -> None:
    """Two ideas the model rates identically must not reorder between runs, or the
    same day's pass produces different trades on a restart."""
    from src.strategies.llm_swing.pipeline import SwingCandidate, select_best_candidates

    a = SwingCandidate("AAA", 100, 95, 110, 0.5, "")
    b = SwingCandidate("BBB", 100, 95, 110, 0.5, "")
    assert [c.symbol for c in select_best_candidates([b, a], 2)] == ["AAA", "BBB"]
    assert [c.symbol for c in select_best_candidates([a, b], 2)] == ["AAA", "BBB"]


def test_internal_features_never_reach_the_prompt() -> None:
    """_features rides along for ranking/logging only. Serialising it would duplicate
    the whole universe inside the prompt and burn the token budget on data the model
    has already been given."""
    from src.strategies.llm_swing.pipeline import build_market_context, build_prompt

    ctx = build_market_context(
        regime="STRONG_BEAR",
        vix=11.0,
        rs_top20={"LAURUSLABS"},
        rs_blacklist=set(),
        sector_status={"PHARMA": "LEADING"},
        last_close=_LAST_CLOSE,
        features={"LAURUSLABS": {"symbol": "LAURUSLABS", "rsi14": 65.8}},
    )
    assert "_features" in ctx
    assert "_features" not in build_prompt(ctx, [], 14)
