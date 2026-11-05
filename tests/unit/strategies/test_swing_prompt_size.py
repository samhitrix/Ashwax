"""The swing prompt must only carry fundamentals for symbols the LLM may actually propose."""

from __future__ import annotations

import json

from src.strategies.llm_swing.pipeline import build_market_context, build_prompt


def _fund(i: int) -> dict:
    return {"pe": 20.0 + i, "roe": 15.0, "notes": "x" * 300}


def _ctx(n_cache: int = 500, candidates: tuple[str, ...] = ("AAA", "BBB", "CCC")):
    cache = {f"SYM{i}": _fund(i) for i in range(n_cache)}
    cache.update({c: _fund(0) for c in candidates})
    return build_market_context(
        regime="STRONG_BEAR",
        vix=11.0,
        rs_top20=set(candidates),
        rs_blacklist=set(),
        sector_status={},
        last_close={c: 100.0 for c in candidates},
        fundamentals=cache,
        features={},
    )


def test_fundamentals_are_scoped_to_the_candidate_universe() -> None:
    ctx = _ctx()
    assert set(ctx["fundamentals"]) == {"AAA", "BBB", "CCC"}


def test_prompt_size_does_not_grow_with_the_size_of_the_fundamentals_cache() -> None:
    small = len(build_prompt(_ctx(n_cache=10), [], 14))
    huge = len(build_prompt(_ctx(n_cache=2000), [], 14))
    assert huge == small  # 2000 cached symbols cost nothing extra
    assert huge < 20_000  # regression guard: was ~163,000 chars (~42k tokens) on 2026-09-24


def test_no_fundamentals_key_when_no_candidate_has_any() -> None:
    ctx = build_market_context(
        regime="X",
        vix=1.0,
        rs_top20={"AAA"},
        rs_blacklist=set(),
        sector_status={},
        last_close={"AAA": 1.0},
        fundamentals={"ZZZ": {"pe": 1}},
        features={},
    )
    assert "fundamentals" not in ctx
    json.dumps({k: v for k, v in ctx.items() if not k.startswith("_")})  # serialisable
