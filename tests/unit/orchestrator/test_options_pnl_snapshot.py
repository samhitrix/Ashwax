"""_capture_options_pnl_snapshot -- the once-daily baseline capture (migration
086) that lets the dashboard compute Today's P&L for positions opened on an
earlier day."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from src.options.structure_selector import OptionLeg


class _Pos:
    def __init__(self, position_id: str, legs: list[OptionLeg]) -> None:
        self.position_id = position_id
        self.protection_legs = [leg for leg in legs if leg.direction == "BUY"]
        self.income_legs = [leg for leg in legs if leg.direction == "SELL"]


class _FakeDesk:
    def __init__(self, positions: dict[str, _Pos]) -> None:
        self.positions = positions


class _FakeRepo:
    def __init__(self) -> None:
        self.saved: tuple[date, dict[str, float]] | None = None

    async def save_daily_pnl_snapshots(
        self, snapshot_date: date, pnl_by_position: dict[str, float]
    ) -> None:
        self.saved = (snapshot_date, dict(pnl_by_position))


class _FakeBroker:
    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = prices

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return {s: self._prices[s] for s in symbols if s in self._prices}


def _leg(sym, kind, strike, direction, premium):
    return OptionLeg(
        tradingsymbol=sym,
        instrument_type=kind,
        strike=strike,
        direction=direction,
        lots=1,
        lot_size=75,
        premium=premium,
    )


@pytest.mark.asyncio
async def test_captures_pnl_for_every_open_position(monkeypatch):
    from src import orchestrator

    legs = [_leg("A", "PE", 22000, "BUY", 50.0), _leg("B", "PE", 22250, "SELL", 92.78)]
    desk = _FakeDesk({"OPT-1": _Pos("OPT-1", legs)})
    repo = _FakeRepo()
    broker = _FakeBroker({"A": 45.0, "B": 88.0})
    shared: dict[str, Any] = {"seller_options": {"desk": desk, "repo": repo}, "broker": broker}

    n = await orchestrator._capture_options_pnl_snapshot(shared, date(2026, 10, 8))

    assert n == 1
    assert repo.saved is not None
    saved_date, saved_pnl = repo.saved
    assert saved_date == date(2026, 10, 8)
    # BUY: (45-50)*75=-375. SELL: (92.78-88)*75=358.5. Total=-16.5
    assert saved_pnl["OPT-1"] == pytest.approx(-16.5)


@pytest.mark.asyncio
async def test_position_with_unknown_ltp_is_skipped_not_zeroed(monkeypatch):
    from src import orchestrator

    legs = [_leg("A", "PE", 22000, "BUY", 50.0)]
    desk = _FakeDesk({"OPT-1": _Pos("OPT-1", legs)})
    repo = _FakeRepo()
    broker = _FakeBroker({})  # no price for "A"
    shared: dict[str, Any] = {"seller_options": {"desk": desk, "repo": repo}, "broker": broker}

    n = await orchestrator._capture_options_pnl_snapshot(shared, date(2026, 10, 8))

    assert n == 0
    assert repo.saved == (date(2026, 10, 8), {})


@pytest.mark.asyncio
async def test_no_open_positions_saves_empty_snapshot(monkeypatch):
    from src import orchestrator

    desk = _FakeDesk({})
    repo = _FakeRepo()
    broker = _FakeBroker({})
    shared: dict[str, Any] = {"seller_options": {"desk": desk, "repo": repo}, "broker": broker}

    n = await orchestrator._capture_options_pnl_snapshot(shared, date(2026, 10, 8))

    assert n == 0
    assert repo.saved == (date(2026, 10, 8), {})


@pytest.mark.asyncio
async def test_missing_desk_or_repo_returns_zero_without_raising():
    from src import orchestrator

    assert await orchestrator._capture_options_pnl_snapshot({}, date(2026, 10, 8)) == 0
    assert (
        await orchestrator._capture_options_pnl_snapshot(
            {"seller_options": {"desk": None, "repo": None}}, date(2026, 10, 8)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_broker_ltp_failure_is_swallowed_snapshot_still_attempted(monkeypatch):
    from src import orchestrator

    legs = [_leg("A", "PE", 22000, "BUY", 50.0)]
    desk = _FakeDesk({"OPT-1": _Pos("OPT-1", legs)})
    repo = _FakeRepo()

    class _RaisingBroker:
        async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
            raise RuntimeError("kite down")

    shared: dict[str, Any] = {
        "seller_options": {"desk": desk, "repo": repo},
        "broker": _RaisingBroker(),
    }

    n = await orchestrator._capture_options_pnl_snapshot(shared, date(2026, 10, 8))

    assert n == 0  # no LTP -> pnl unknown -> skipped, but no exception propagates
    assert repo.saved == (date(2026, 10, 8), {})
