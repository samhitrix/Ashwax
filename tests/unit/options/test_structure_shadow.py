"""structure_shadow: snapshot rows, once-per-day dedupe, and the never-raises/never-trades contract."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from src.options import structure_shadow as ss
from src.options.structure_selector import OptionLeg

TODAY = date(2030, 1, 7)


def _leg(kind: str, strike: float, direction: str, mid: float) -> OptionLeg:
    return OptionLeg(f"TEST{kind}{int(strike)}", kind, strike, direction, 1, 50, mid, 0.2)


def _chain(quotes: dict[tuple[str, float], tuple[float, float]]) -> SimpleNamespace:
    """Chain stub: quotes[(kind, strike)] = (bid, ask)."""
    strikes: dict[float, SimpleNamespace] = {}
    for (kind, strike), (bid, ask) in quotes.items():
        row = strikes.setdefault(
            strike,
            SimpleNamespace(
                strike=strike,
                call=None,
                put=None,
                call_iv=0.2,
                put_iv=0.2,
                call_delta=0.25,
                put_delta=-0.25,
            ),
        )
        q = SimpleNamespace(bid=bid, ask=ask, ltp=(bid + ask) / 2, oi=0, volume=0)
        if kind == "CE":
            row.call = q
        else:
            row.put = q
    return SimpleNamespace(
        strikes=list(strikes.values()),
        expiry=datetime(2030, 2, 6, tzinfo=UTC),
        spot=100.0,
        days_to_expiry=30,
    )


def test_realistic_credit_is_below_mid_credit() -> None:
    legs = [_leg("PE", 90, "SELL", 2.0), _leg("PE", 80, "BUY", 0.5)]
    chain = _chain({("PE", 90): (1.8, 2.2), ("PE", 80): (0.4, 0.6)})
    rows = ss.leg_rows(legs, chain)  # type: ignore[arg-type]
    credit_mid, credit_real = ss.credits(rows)
    assert credit_mid == 1.5
    assert credit_real == pytest.approx(1.8 - 0.6)  # sell at bid, buy at ask
    assert credit_real < credit_mid


def test_max_loss_of_condor_is_width_minus_credit() -> None:
    # put wing 80/90, call wing 110/120, 1.0 total realistic credit, width 10 each side
    rows = [
        {"kind": "PE", "strike": 90.0, "dir": "SELL", "mid": 1, "bid": 1, "ask": 1},
        {"kind": "PE", "strike": 80.0, "dir": "BUY", "mid": 0, "bid": 0, "ask": 0},
        {"kind": "CE", "strike": 110.0, "dir": "SELL", "mid": 1, "bid": 1, "ask": 1},
        {"kind": "CE", "strike": 120.0, "dir": "BUY", "mid": 0, "bid": 0, "ask": 0},
    ]
    credit_real = 2.0
    assert ss.max_loss_per_share(rows, credit_real, calendar=False) == pytest.approx(8.0)
    assert ss.expiry_pnl(rows, 100.0, credit_real) == pytest.approx(
        2.0
    )  # inside the wings: keep all
    assert ss.expiry_pnl(rows, 70.0, credit_real) == pytest.approx(-8.0)  # through the put wing


def test_calendar_max_loss_is_the_debit() -> None:
    rows = [{"kind": "CE", "strike": 100.0, "dir": "BUY", "mid": 3, "bid": 3, "ask": 3}]
    assert ss.max_loss_per_share(rows, -3.0, calendar=True) == 3.0
    assert ss.max_loss_per_share(rows, 1.0, calendar=True) == 0.0


def _desk(chain: SimpleNamespace | None) -> SimpleNamespace:
    async def fetch_tier2(_und: str, buckets: list[int]) -> dict[int, SimpleNamespace]:
        return {} if chain is None else {b: chain for b in buckets}

    return SimpleNamespace(_chain_fetcher=SimpleNamespace(fetch_tier2=fetch_tier2))


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ss._DONE.clear()
    ss._SKIP_LOGGED.clear()
    monkeypatch.setattr(ss, "_OUT", tmp_path / "shadow.jsonl")
    monkeypatch.setattr(
        ss._seller_settings,
        "structure_shadow",
        lambda: {"enabled": True, "types": ["bull_put_spread"]},
    )


def _run(desk: SimpleNamespace) -> None:
    asyncio.run(
        ss.record_cycle(
            desk,  # type: ignore[arg-type]
            underlying="NSE:TESTIDX",
            regime="UNDEFINED",
            ivr=20.0,
            term_structure="contango",
            vix=18.0,
            today=TODAY,
            expiry_calendar={},
        )
    )


def test_disabled_flag_is_a_noop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(ss._seller_settings, "structure_shadow", lambda: {"enabled": False})
    _run(_desk(None))
    assert not ss._OUT.exists()


def test_no_chain_does_not_raise_and_retries_next_cycle() -> None:
    _run(_desk(None))
    assert not ss._OUT.exists()
    assert (TODAY.isoformat(), "NSE:TESTIDX", "bull_put_spread") not in ss._DONE


def test_unbuildable_structure_is_retried_and_reason_logged_once(
    monkeypatch,
    caplog,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(ss, "build_structure", lambda *_a, **_k: None)
    chain = _chain({("PE", 90): (1.8, 2.2)})
    with caplog.at_level("INFO", logger="options.seller_desk"):
        _run(_desk(chain))
        _run(_desk(chain))  # next cycle: retried, but the reason is not logged again
    skips = [r for r in caplog.records if "structure_shadow_skip" in r.getMessage()]
    assert len(skips) == 1 and "reason=" in skips[0].getMessage()
    assert "liquid=" in skips[0].getMessage()  # says WHY (in-tolerance vs liquid strikes)
    assert not ss._OUT.exists()
    assert (TODAY.isoformat(), "NSE:TESTIDX", "bull_put_spread") not in ss._DONE


def test_restart_does_not_write_a_second_row_for_the_same_day(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    legs = ([_leg("PE", 80, "BUY", 0.5)], [_leg("PE", 90, "SELL", 2.0)])
    monkeypatch.setattr(ss, "build_structure", lambda *_a, **_k: legs)
    chain = _chain({("PE", 90): (1.8, 2.2), ("PE", 80): (0.4, 0.6)})
    _run(_desk(chain))
    # simulate a launcher restart: in-memory state is gone, only the file remains
    ss._DONE.clear()
    ss._SEEDED_FROM = None
    _run(_desk(chain))
    assert len(ss._OUT.read_text().splitlines()) == 1


def test_builder_error_is_swallowed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("builder blew up")

    monkeypatch.setattr(ss, "build_structure", boom)
    _run(_desk(_chain({("PE", 90): (1, 1)})))  # must not raise
    assert not ss._OUT.exists()


def test_snapshot_written_once_per_day(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    legs = ([_leg("PE", 80, "BUY", 0.5)], [_leg("PE", 90, "SELL", 2.0)])
    monkeypatch.setattr(ss, "build_structure", lambda *_a, **_k: legs)
    chain = _chain({("PE", 90): (1.8, 2.2), ("PE", 80): (0.4, 0.6)})
    _run(_desk(chain))
    _run(_desk(chain))  # same day: no second row
    lines = ss._OUT.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["structure"] == "bull_put_spread" and row["regime"] == "UNDEFINED"
    assert row["credit_realistic"] == pytest.approx(1.2) and row["max_loss"] == pytest.approx(8.8)
