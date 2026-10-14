"""Tests for _evaluate_llm_intraday_signal — the scan-loop step that turns a
published LLM intraday idea into a Signal. The idea was reasoned from a snapshot
up to idea_ttl_minutes old, so it must be refused once stale, once the agent's
day cap is used, after last_entry_by, or once price has moved far enough that the
trade would no longer be the one that was validated."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import src.orchestrator as orch
from src.orchestrator import _evaluate_llm_intraday_signal
from src.strategies.uss.schema import Direction

_IST = ZoneInfo("Asia/Kolkata")
_NOW = datetime(2026, 9, 11, 10, 50, tzinfo=_IST)
_COMPILED = SimpleNamespace(spec=SimpleNamespace(meta=SimpleNamespace(name="llm_intraday_agent")))
_CFG = {
    "idea_ttl_minutes": 15,
    "max_trades_per_day": 2,
    "entry_tolerance_pct": 0.4,
    "min_stop_pct": 0.5,
    "min_stop_atr_mult": 2.0,
    "max_stop_pct": 2.5,
    "min_reward_risk": 1.5,
    "min_target_pct": 0.6,
}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orch, "_cfg", lambda name: {"llm_intraday": _CFG} if name == "global" else {}
    )


def _idea(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "RELIANCE",
        "direction": "LONG",
        "entry_price": 1000.0,
        "stop_loss": 993.0,
        "target": 1012.0,
        "confidence": 0.7,
        "reasoning": "held VWAP",
        "atr14_pct": 0.2,
        "ts": _NOW.timestamp() - 120,
    }
    base.update(kw)
    return base


def _shared(idea: dict[str, Any] | None, **kw: Any) -> dict[str, Any]:
    shared: dict[str, Any] = {
        "llm_intraday_ideas": {"RELIANCE": idea} if idea else {},
        "crm_ctx": SimpleNamespace(last_entry_by_str="14:30"),
    }
    shared.update(kw)
    return shared


def _df(close: float) -> pd.DataFrame:
    return pd.DataFrame({"close": [close - 1.0, close]})


def _eval(shared: dict[str, Any], close: float = 1000.5, now: datetime = _NOW) -> Any:
    return _evaluate_llm_intraday_signal(_COMPILED, shared, "NSE:RELIANCE", _df(close), now)


def test_fresh_idea_becomes_signal_at_latest_close() -> None:
    sig = _eval(_shared(_idea()))
    assert sig is not None
    assert sig.direction == Direction.LONG
    assert sig.entry_price == 1000.5  # latest close, not the LLM's stale entry
    # sl/target shift by the idea's original distance (7, 12) from the new
    # entry, not the frozen 993/1012 — preserves the validated R:R (2026-09-21
    # fix: placing at the frozen levels here would trade the worse R:R that
    # still_valid_at's shifted re-check was supposed to have ruled out).
    assert (sig.sl, sig.target) == (993.5, 1012.5)
    assert sig.confidence == 0.7


def test_short_idea() -> None:
    idea = _idea(direction="SHORT", stop_loss=1007.0, target=988.0)
    sig = _eval(_shared(idea), close=999.8)
    assert sig is not None and sig.direction == Direction.SHORT


@pytest.mark.parametrize(
    ("shared_kw", "idea_kw", "close", "now"),
    [
        ({}, {"ts": _NOW.timestamp() - 16 * 60}, 1000.5, _NOW),  # stale (> 15 min)
        ({"llm_intraday_traded": {"2026-09-11": 2}}, {}, 1000.5, _NOW),  # day cap used
        ({}, {}, 1000.5, _NOW.replace(hour=14, minute=30)),  # at last_entry_by
        ({}, {}, 1005.0, _NOW),  # 0.5% away from entry > 0.4% tolerance
    ],
)
def test_refused(
    shared_kw: dict[str, Any], idea_kw: dict[str, Any], close: float, now: datetime
) -> None:
    assert _eval(_shared(_idea(**idea_kw), **shared_kw), close=close, now=now) is None


def test_small_drift_shifts_levels_instead_of_refusing() -> None:
    """2026-09-21 fix: a small within-tolerance drift (-0.35%, still inside
    entry_tolerance_pct) used to be refused because the re-check compared the
    frozen 993 stop against the new price, leaving only ~0.35% of remaining
    stop distance — 'noise' by the stop floor. It's now accepted with the
    stop/target shifted to preserve the original 7pt/12pt distances (and
    R:R), matching how a professional actually manages a small fill drift."""
    sig = _eval(_shared(_idea()), close=996.5)
    assert sig is not None
    assert (sig.sl, sig.target) == (989.5, 1008.5)


def test_no_idea_for_symbol() -> None:
    assert _eval(_shared(None)) is None


# ── _reseed_llm_intraday_ideas ──────────────────────────────────────────────
# 2026-09-23: shared["llm_intraday_ideas"] is in-memory only. The launcher
# restarts the orchestrator whenever its hang-watchdog fires; on 2026-09-23 a
# slow LLM call tripped it mid-session and silently erased two just-proposed
# ideas (IIFL, AEGISVOPAK) before the 5-min scan ever evaluated them, though
# they were sitting in llm_intraday_journal the whole time. These guard the
# fix: restore from the journal on startup, same class as the OrderMonitor
# reseed that already exists for open positions.


class _FakeJournalRepo:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def todays_runs(self, run_date: Any) -> list[dict[str, Any]]:
        return self._rows


def _row(*ideas: dict[str, Any]) -> dict[str, Any]:
    return {"ideas_json": list(ideas)}


@pytest.mark.asyncio
async def test_reseed_restores_still_fresh_idea() -> None:
    shared: dict[str, Any] = {
        "llm_intraday_journal_repo": _FakeJournalRepo([_row(_idea(symbol="IIFL"))])
    }
    n = await orch._reseed_llm_intraday_ideas(shared, now=_NOW)
    assert n == 1
    assert shared["llm_intraday_ideas"]["IIFL"]["symbol"] == "IIFL"


@pytest.mark.asyncio
async def test_reseed_drops_expired_idea() -> None:
    """An idea that would have expired anyway during the outage must not be
    resurrected with a stale clock -- restoring it is not a free pass around
    the TTL check every live idea still has to clear."""
    stale = _idea(symbol="OLDIDEA", ts=_NOW.timestamp() - 20 * 60)  # 20 min > 15-min TTL
    shared: dict[str, Any] = {"llm_intraday_journal_repo": _FakeJournalRepo([_row(stale)])}
    n = await orch._reseed_llm_intraday_ideas(shared, now=_NOW)
    assert n == 0
    assert shared["llm_intraday_ideas"] == {}


@pytest.mark.asyncio
async def test_reseed_newest_run_wins_for_same_symbol() -> None:
    older = _idea(symbol="IIFL", entry_price=630.0, ts=_NOW.timestamp() - 600)
    newer = _idea(symbol="IIFL", entry_price=636.95, ts=_NOW.timestamp() - 60)
    shared: dict[str, Any] = {
        "llm_intraday_journal_repo": _FakeJournalRepo([_row(older), _row(newer)])
    }
    await orch._reseed_llm_intraday_ideas(shared, now=_NOW)
    assert shared["llm_intraday_ideas"]["IIFL"]["entry_price"] == 636.95


@pytest.mark.asyncio
async def test_reseed_no_repo_is_a_noop() -> None:
    shared: dict[str, Any] = {}
    n = await orch._reseed_llm_intraday_ideas(shared, now=_NOW)
    assert n == 0
    assert shared.get("llm_intraday_ideas") is None
