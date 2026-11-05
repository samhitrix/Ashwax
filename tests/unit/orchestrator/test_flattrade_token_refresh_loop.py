"""Flattrade token loop: validate only (never logs in), pick up a manual-login token, alert
once per IST day, and do nothing when Flattrade is unused or has no credentials."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import src.orchestrator as orch_mod
from src.brokers import flattrade_auth
from src.brokers.adapters.flattrade.client import FlattradeError
from src.infrastructure import operator_alerts
from src.market.calendar import is_trading_day, load_holidays
from src.market_data.provider_config import ProviderConfig
from src.market_data.router import MarketDataRouter

_IST = ZoneInfo("Asia/Kolkata")
TRADING_DAY_10AM = datetime(2026, 10, 13, 10, 0, tzinfo=_IST)  # a Tuesday


def _expired(*_a: Any, **_k: Any) -> str:
    raise RuntimeError("Flattrade token expired; run python -m src.brokers.flattrade_auth")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, datetime]:
    """The loop's IST clock; defaults to 10:00 on a trading day."""
    assert is_trading_day(TRADING_DAY_10AM.date())
    now = {"t": TRADING_DAY_10AM}
    monkeypatch.setattr(orch_mod, "_flattrade_now", lambda: now["t"])
    return now


class _FakeFlattrade:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    def set_access_token(self, token: str) -> None:
        self.tokens.append(token)


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLATTRADE_API_KEY", "k")
    monkeypatch.setenv("FLATTRADE_USER_ID", "u")


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    raised: list[str] = []

    def _raise(message: str, **kw: Any) -> bool:
        raised.append(kw["source"])
        return True

    monkeypatch.setattr(operator_alerts, "raise_alert", _raise)
    return raised


async def _run_checks(shared: dict[str, Any], monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    """Run the loop for exactly n checks (sleep is replaced; stop after the n-th)."""
    stop = asyncio.Event()
    count = {"n": 0}

    async def _sleep(_s: float) -> None:
        count["n"] += 1
        if count["n"] >= n:
            stop.set()

    monkeypatch.setattr(orch_mod.asyncio, "sleep", _sleep)
    await asyncio.wait_for(orch_mod._flattrade_token_refresh_loop(shared, stop), 5.0)


async def test_noop_when_flattrade_not_used(
    monkeypatch: pytest.MonkeyPatch, creds: None, alerts: list[str]
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(flattrade_auth, "auto_refresh", lambda *a, **k: calls.append(1) or "t")
    await asyncio.wait_for(orch_mod._flattrade_token_refresh_loop({}, asyncio.Event()), 2.0)
    assert calls == [] and alerts == []


async def test_noop_without_credentials(monkeypatch: pytest.MonkeyPatch, alerts: list[str]) -> None:
    monkeypatch.delenv("FLATTRADE_API_KEY", raising=False)
    monkeypatch.delenv("FLATTRADE_USER_ID", raising=False)
    calls: list[int] = []
    monkeypatch.setattr(flattrade_auth, "auto_refresh", lambda *a, **k: calls.append(1) or "t")
    b = _FakeFlattrade()
    shared = {"flattrade_token_targets": [b.set_access_token]}
    await asyncio.wait_for(orch_mod._flattrade_token_refresh_loop(shared, asyncio.Event()), 2.0)
    assert calls == [] and b.tokens == [] and alerts == []


async def test_picks_up_new_token_via_router_set_provider_token(
    monkeypatch: pytest.MonkeyPatch, creds: None, alerts: list[str]
) -> None:
    provider = _FakeFlattrade()
    router = MarketDataRouter({"flattrade": provider}, ProviderConfig(("flattrade",), 3, 300, {}))
    live = _FakeFlattrade()
    tokens = iter(["OLD", "OLD", "NEW"])
    monkeypatch.setattr(flattrade_auth, "auto_refresh", lambda *a, **k: next(tokens))
    shared = {
        "flattrade_token_targets": [
            lambda t: router.set_provider_token("flattrade", t),
            live.set_access_token,
        ]
    }
    await _run_checks(shared, monkeypatch, 3)
    # pushed once per distinct token, to the router provider AND the live broker
    assert provider.tokens == ["OLD", "NEW"]
    assert live.tokens == ["OLD", "NEW"]
    assert alerts == []


async def test_invalid_token_alerts_once_per_day_and_never_logs_in(
    monkeypatch: pytest.MonkeyPatch, creds: None, alerts: list[str], clock: Any
) -> None:
    def _no_headless(*_a: Any, **_k: Any) -> str:
        raise AssertionError("headless login hook must never be called")

    monkeypatch.setattr(flattrade_auth, "auto_refresh", _expired)
    monkeypatch.setattr(orch_mod, "_flattrade_headless_login", _no_headless)
    b = _FakeFlattrade()
    await _run_checks({"flattrade_token_targets": [b.set_access_token]}, monkeypatch, 3)
    assert alerts == ["flattrade_token_invalid"]  # 3 failed checks, one alert
    assert b.tokens == []


async def test_api_error_is_not_reported_as_expired(
    monkeypatch: pytest.MonkeyPatch, creds: None, alerts: list[str]
) -> None:
    def _down(*_a: Any, **_k: Any) -> str:
        raise FlattradeError("timeout")

    monkeypatch.setattr(flattrade_auth, "auto_refresh", _down)
    await _run_checks(
        {"flattrade_token_targets": [_FakeFlattrade().set_access_token]}, monkeypatch, 2
    )
    assert alerts == []


def test_headless_login_hook_is_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        orch_mod._flattrade_headless_login()


# ── alert window (review I3) ─────────────────────────────────────────────────


def _first_weekday_holiday() -> datetime:
    d = next(h for h in sorted(load_holidays()) if h.weekday() < 5)
    return datetime(d.year, d.month, d.day, 10, 0, tzinfo=_IST)


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 10, 17, 10, 0, tzinfo=_IST),  # Saturday
        datetime(2026, 10, 18, 10, 0, tzinfo=_IST),  # Sunday
        datetime(2026, 10, 13, 22, 0, tzinfo=_IST),  # trading day, night
        datetime(2026, 10, 13, 7, 59, tzinfo=_IST),  # trading day, before 08:00
        datetime(2026, 10, 13, 15, 31, tzinfo=_IST),  # trading day, after the close
    ],
)
async def test_no_alert_outside_trading_days_or_window(
    monkeypatch: pytest.MonkeyPatch, creds: None, alerts: list[str], clock: Any, when: datetime
) -> None:
    clock["t"] = when
    monkeypatch.setattr(flattrade_auth, "auto_refresh", _expired)
    await _run_checks(
        {"flattrade_token_targets": [_FakeFlattrade().set_access_token]}, monkeypatch, 2
    )
    assert alerts == []


async def test_no_alert_on_an_nse_holiday(
    monkeypatch: pytest.MonkeyPatch, creds: None, alerts: list[str], clock: Any
) -> None:
    clock["t"] = _first_weekday_holiday()
    monkeypatch.setattr(flattrade_auth, "auto_refresh", _expired)
    await _run_checks(
        {"flattrade_token_targets": [_FakeFlattrade().set_access_token]}, monkeypatch, 2
    )
    assert alerts == []


async def test_alert_at_open_of_window_after_a_quiet_night(
    monkeypatch: pytest.MonkeyPatch, creds: None, alerts: list[str], clock: Any
) -> None:
    """Expired overnight: silent at 07:55, one alert once the 08:00 window opens."""
    times = iter(
        [
            datetime(2026, 10, 13, 7, 55, tzinfo=_IST),
            datetime(2026, 10, 13, 8, 0, tzinfo=_IST),
            datetime(2026, 10, 13, 8, 5, tzinfo=_IST),
        ]
    )
    monkeypatch.setattr(orch_mod, "_flattrade_now", lambda: next(times))
    monkeypatch.setattr(flattrade_auth, "auto_refresh", _expired)
    await _run_checks(
        {"flattrade_token_targets": [_FakeFlattrade().set_access_token]}, monkeypatch, 3
    )
    assert alerts == ["flattrade_token_invalid"]


async def test_second_expiry_same_day_alerts_again_after_a_valid_check(
    monkeypatch: pytest.MonkeyPatch, creds: None, alerts: list[str], clock: Any
) -> None:
    outcomes = iter(["expired", "TOKEN", "expired", "expired"])

    def _check(*_a: Any, **_k: Any) -> str:
        out = next(outcomes)
        if out == "expired":
            _expired()
        return out

    monkeypatch.setattr(flattrade_auth, "auto_refresh", _check)
    await _run_checks(
        {"flattrade_token_targets": [_FakeFlattrade().set_access_token]}, monkeypatch, 4
    )
    assert alerts == ["flattrade_token_invalid", "flattrade_token_invalid"]


async def test_import_failure_is_logged_not_silent(
    monkeypatch: pytest.MonkeyPatch, creds: None, alerts: list[str]
) -> None:
    errors: list[str] = []
    monkeypatch.setitem(sys.modules, "src.brokers.adapters.flattrade.client", None)
    monkeypatch.setattr(orch_mod._log, "error", lambda msg, *a, **k: errors.append(msg % a))
    shared = {"flattrade_token_targets": [_FakeFlattrade().set_access_token]}
    await asyncio.wait_for(orch_mod._flattrade_token_refresh_loop(shared, asyncio.Event()), 2.0)
    assert errors and "flattrade_token_refresh_loop_disabled" in errors[0]
