"""Smart SL (confirmed stop) for the short-premium engine.

A touch of the stop only ARMS the leg; it exits when the price has stayed at/above the stop for
``smart_sl_confirm_seconds``, or instantly at the hard stop (sl_pct x smart_sl_hard_multiple). A dip
back below the stop disarms it, so a one-print spike no longer closes the leg.
Fixture: entry premium 40, sl 23% -> soft stop 49.2; hard 1.5x -> 34.5% -> 53.8.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from src.options import short_premium as st

from tests.unit.options.test_options_intraday_short_premium import (
    _CFG,
    _IST,
    _MON,
    _new_desk,
    _open,
)

_SMART: dict[str, Any] = {
    **_CFG,
    "smart_sl": True,
    "smart_sl_confirm_seconds": 60,
    "smart_sl_hard_multiple": 1.5,
}
_T0 = datetime(2026, 10, 12, 11, 0, tzinfo=_IST)


def _at(seconds: int) -> datetime:
    return _T0 + timedelta(seconds=seconds)


async def _tick(desk, prices, seconds, raw=None):  # type: ignore[no-untyped-def]
    await st.manage(desk, _MON, _at(seconds), prices, st.parse_cfg(raw or _SMART))


def _open_syms(pos) -> list[str]:  # type: ignore[no-untyped-def]
    return sorted(leg.tradingsymbol for leg in pos.income_legs)


def test_smart_sl_is_off_by_default_with_sane_defaults() -> None:
    cfg = st.parse_cfg(_CFG)
    assert cfg.smart_sl is False
    assert cfg.smart_sl_confirm_seconds == 60 and cfg.smart_sl_hard_multiple == 1.5


@pytest.mark.parametrize(
    "bad",
    [
        {"smart_sl_confirm_seconds": 0},
        {"smart_sl_confirm_seconds": 301},
        {"smart_sl_hard_multiple": 1.0},
        {"smart_sl_hard_multiple": 3.5},
    ],
)
def test_invalid_smart_sl_config_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        st.parse_cfg({**_SMART, **bad})


async def test_spike_then_revert_does_not_close_the_leg() -> None:
    desk, ex = _new_desk()
    pos = _open(desk)
    await _tick(desk, {"SC": 49.5, "SP": 35.0}, 0)  # touches 49.2 -> armed only
    assert _open_syms(pos) == ["SC", "SP"] and ex.closed == []
    assert "SC" in pos.meta["smart_sl"]["armed"]
    await _tick(desk, {"SC": 45.0, "SP": 35.0}, 20)  # back below -> disarmed
    assert "SC" not in pos.meta["smart_sl"]["armed"]
    assert pos.meta["smart_sl"]["avoided"][0]["leg"] == "SC"  # legacy stop would have fired here
    await _tick(desk, {"SC": 44.0, "SP": 35.0}, 120)
    assert _open_syms(pos) == ["SC", "SP"] and ex.closed == []


async def test_price_held_above_stop_for_confirm_window_closes_the_leg() -> None:
    desk, _ = _new_desk()
    pos = _open(desk)
    await _tick(desk, {"SC": 49.5, "SP": 35.0}, 0)
    await _tick(desk, {"SC": 49.6, "SP": 35.0}, 30)
    assert _open_syms(pos) == ["SC", "SP"]  # 30s held: not yet
    await _tick(desk, {"SC": 49.3, "SP": 35.0}, 60)
    assert _open_syms(pos) == ["SP"]
    reason = pos.closed_legs[0]["reason"]
    assert reason.startswith("SHORT_PREMIUM_SL") and "confirmed" in reason


async def test_hard_stop_closes_instantly_without_waiting() -> None:
    desk, _ = _new_desk()
    pos = _open(desk)
    await _tick(desk, {"SC": 54.0, "SP": 35.0}, 0)  # >= 53.8 hard stop, first tick
    assert _open_syms(pos) == ["SP"]
    assert "hard" in pos.closed_legs[0]["reason"]


async def test_dip_below_stop_restarts_the_timer() -> None:
    desk, _ = _new_desk()
    pos = _open(desk)
    await _tick(desk, {"SC": 49.5, "SP": 35.0}, 0)
    await _tick(desk, {"SC": 45.0, "SP": 35.0}, 30)  # disarm
    await _tick(desk, {"SC": 49.5, "SP": 35.0}, 40)  # re-arm at 40s
    await _tick(desk, {"SC": 49.5, "SP": 35.0}, 90)  # only 50s since re-arm
    assert _open_syms(pos) == ["SC", "SP"]
    await _tick(desk, {"SC": 49.5, "SP": 35.0}, 100)
    assert _open_syms(pos) == ["SP"]


async def test_armed_state_is_persisted_and_survives_a_restart() -> None:
    desk, _ = _new_desk()
    pos = _open(desk)
    await _tick(desk, {"SC": 49.5, "SP": 35.0}, 0)
    assert desk._repo.metas and "SC" in desk._repo.metas[-1]["smart_sl"]["armed"]
    # restart: a fresh desk rebuilt from the persisted meta keeps the original arm time
    desk2, _ = _new_desk()
    pos2 = _open(desk2)
    pos2.meta = desk._repo.metas[-1]
    await _tick(desk2, {"SC": 49.5, "SP": 35.0}, 60)
    assert _open_syms(pos2) == ["SP"]


async def test_missing_price_neither_arms_nor_disarms() -> None:
    desk, _ = _new_desk()
    pos = _open(desk)
    await _tick(desk, {"SP": 35.0}, 0)  # no SC price
    assert "SC" not in pos.meta.get("smart_sl", {}).get("armed", {})
    await _tick(desk, {"SC": 49.5, "SP": 35.0}, 10)
    await _tick(desk, {"SP": 35.0}, 30)  # blind tick keeps the arm
    assert "SC" in pos.meta["smart_sl"]["armed"]
    await _tick(desk, {"SC": 49.5, "SP": 35.0}, 70)
    assert _open_syms(pos) == ["SP"]


async def test_switch_off_keeps_first_touch_stop() -> None:
    desk, _ = _new_desk()
    pos = _open(desk)
    await _tick(desk, {"SC": 49.5, "SP": 35.0}, 0, raw={**_SMART, "smart_sl": False})
    assert _open_syms(pos) == ["SP"]


async def test_no_config_falls_back_to_first_touch_stop() -> None:
    desk, _ = _new_desk()
    pos = _open(desk)
    await st.manage(desk, _MON, _T0, {"SC": 49.5, "SP": 35.0}, None)
    assert _open_syms(pos) == ["SP"]


async def test_failed_confirmed_close_retries_next_tick_without_rearming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(st, "_alert", lambda desk, **kw: None)
    desk, ex = _new_desk()
    pos = _open(desk)
    await _tick(desk, {"SC": 49.5, "SP": 35.0}, 0)
    ex.fail_next = True
    await _tick(desk, {"SC": 49.5, "SP": 35.0}, 60)  # confirmed, broker refuses
    assert _open_syms(pos) == ["SC", "SP"] and "SC" in pos.meta["smart_sl"]["armed"]
    await _tick(desk, {"SC": 49.5, "SP": 35.0}, 70)  # retried at once, not after another 60s
    assert _open_syms(pos) == ["SP"]


# --- hard-stop confirm window: entry 40, soft 49.2, hard 53.8 (34.5%), cap 2.5x = 63.0 -----------
_HC: dict[str, Any] = {**_SMART, "smart_sl_hard_confirm_seconds": 15, "smart_sl_cap_multiple": 2.5}


def test_hard_confirm_is_off_by_default() -> None:
    assert st.parse_cfg(_SMART).smart_sl_hard_confirm_seconds == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"smart_sl_hard_confirm_seconds": 61},
        {"smart_sl_hard_confirm_seconds": -1},
        {"smart_sl_cap_multiple": 1.5},  # not beyond the hard multiple
        {"smart_sl_cap_multiple": 4.5},
    ],
)
def test_invalid_hard_confirm_config_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        st.parse_cfg({**_HC, **bad})


async def test_hard_level_spike_that_reverts_does_not_close() -> None:
    desk, ex = _new_desk()
    pos = _open(desk)
    await _tick(desk, {"SC": 54.0, "SP": 35.0}, 0, raw=_HC)  # over hard: armed, not closed
    assert _open_syms(pos) == ["SC", "SP"] and ex.closed == []
    await _tick(desk, {"SC": 52.0, "SP": 35.0}, 5, raw=_HC)  # back under hard (still over soft)
    assert "SC" not in pos.meta["smart_sl"]["hard_armed"]
    await _tick(desk, {"SC": 54.0, "SP": 35.0}, 10, raw=_HC)  # re-arms: timer restarts
    await _tick(desk, {"SC": 54.0, "SP": 35.0}, 20, raw=_HC)  # only 10s since re-arm
    assert _open_syms(pos) == ["SC", "SP"]


async def test_hard_level_held_for_confirm_window_closes() -> None:
    desk, _ = _new_desk()
    pos = _open(desk)
    await _tick(desk, {"SC": 54.0, "SP": 35.0}, 0, raw=_HC)
    await _tick(desk, {"SC": 55.0, "SP": 35.0}, 15, raw=_HC)
    assert _open_syms(pos) == ["SP"]
    assert "hard" in pos.closed_legs[0]["reason"] and "confirmed" in pos.closed_legs[0]["reason"]


async def test_cap_closes_instantly_even_with_confirm_window() -> None:
    desk, _ = _new_desk()
    pos = _open(desk)
    await _tick(desk, {"SC": 63.5, "SP": 35.0}, 0, raw=_HC)  # >= 63.0 cap on the first tick
    assert _open_syms(pos) == ["SP"]
    assert "cap" in pos.closed_legs[0]["reason"]


# --- leg telemetry (generic switch, research data only) -----------------------------------------
class _Lvl:
    def __init__(self, price: float) -> None:
        self.price = price


class _Depth:
    bids = [_Lvl(48.9)]
    asks = [_Lvl(49.4)]


class _FakeBroker:
    async def get_ltp(self, symbols):  # type: ignore[no-untyped-def]
        return {s: 25000.0 for s in symbols}

    async def get_depth(self, symbol):  # type: ignore[no-untyped-def]
        return _Depth()


async def test_telemetry_writes_spot_bid_ask_and_arm_state(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(st, "_TELEMETRY_DIR", tmp_path)
    desk, _ = _new_desk()
    pos = _open(desk)
    pos.meta["smart_sl"] = {"armed": {"SC": "x"}, "hard_armed": {}}
    cfg = st.parse_cfg({**_SMART, "leg_telemetry": True})
    await st._log_telemetry(_FakeBroker(), "t", [pos], {"SC": 49.5, "SP": 35.0}, _at(0), cfg)
    lines = (tmp_path / "2026-10-12.csv").read_text().splitlines()
    assert lines[0].startswith("ts,strategy,position_id,leg") and len(lines) == 3
    sc = next(row.split(",") for row in lines[1:] if ",SC," in row)
    assert sc[5:9] == ["49.5", "48.9", "49.4", "25000.0"] and sc[9] == "1"


async def test_telemetry_failure_never_raises(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(st, "_TELEMETRY_DIR", tmp_path)

    class _Boom:
        async def get_ltp(self, symbols):  # type: ignore[no-untyped-def]
            raise RuntimeError("down")

    desk, _ = _new_desk()
    pos = _open(desk)
    await st._log_telemetry(_Boom(), "t", [pos], {}, _at(0), st.parse_cfg(_SMART))
    assert not list(tmp_path.iterdir())


def test_telemetry_is_off_by_default() -> None:
    assert st.parse_cfg(_CFG).leg_telemetry is False
