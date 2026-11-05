"""direction_shadow: parse, context, and the never-raises/never-gates contract."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from src.options import direction_shadow as ds


def test_parse_view_accepts_fenced_json() -> None:
    out = ds.parse_view('```json\n{"view": "bull", "confidence": 0.6, "reason": "x"}\n```')
    assert out == {"view": "BULL", "confidence": 0.6, "reason": "x"}


@pytest.mark.parametrize(
    "bad", ["", "no json", '{"view": "UP", "confidence": 1}', '{"view": "BULL"}']
)
def test_parse_view_rejects_malformed(bad: str) -> None:
    assert ds.parse_view(bad) is None


def _desk(reply: str | Exception) -> SimpleNamespace:
    async def complete(*_a: object, **_k: object) -> str:
        if isinstance(reply, Exception):
            raise reply
        return reply

    llm = SimpleNamespace(complete=complete, last_provider=None)
    return SimpleNamespace(
        _g9_llm=llm, _g9_llm_loaded=True, _day_ref={"NIFTY": (None, 22400.0, None)}
    )


def test_build_context_day_change_and_distance() -> None:
    ctx = ds.build_context(
        _desk("{}"),
        underlying="NIFTY",
        strategy_type="bear_call_spread",
        regime="R",
        ivr=26.04,
        directional_bias=0.00123456,
        vix=14.7,
        short_strike=23200.0,
        spot=22736.0,
        expiry="2026-11-09",
        dte=27,
    )
    assert ctx["day_change_pct_since_first_seen_today"] == pytest.approx(1.5, abs=0.01)
    assert ctx["short_strike_distance_pct"] == pytest.approx(2.04, abs=0.01)


@pytest.mark.asyncio
async def test_record_forecast_writes_row(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ds, "_OUT", tmp_path / "o.jsonl")
    await ds.record_forecast(
        _desk('{"view": "RANGE", "confidence": 0.7, "reason": "r"}'), {"structure": "s"}, "P1"
    )
    row = json.loads((tmp_path / "o.jsonl").read_text())
    assert row["position_id"] == "P1" and row["view"] == "RANGE"


@pytest.mark.asyncio
async def test_record_forecast_never_raises_and_skips_bad(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ds, "_OUT", tmp_path / "o.jsonl")
    await ds.record_forecast(_desk(RuntimeError("provider down")), {"structure": "s"}, "P1")
    await ds.record_forecast(_desk("garbage"), {"structure": "s"}, "P2")
    assert not (tmp_path / "o.jsonl").exists()
