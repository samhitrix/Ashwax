"""Fakes shared by the desk-hosted options strategy tests (expiry-day, intraday short premium)."""

from __future__ import annotations

from src.brokers.base import OptionQuote

from tests.unit.options.test_seller_desk import _APPROVE_ALL_CRM, _FakeRepo


def _q(mid: float) -> OptionQuote:
    return OptionQuote("SYM", ltp=mid, bid=mid - 0.05, ask=mid + 0.05, oi=5000, volume=2000)


class _Repo(_FakeRepo):
    def __init__(self, traded: bool = False) -> None:
        super().__init__()
        self.traded = traded

    async def has_position_for_expiry(self, **kwargs):  # type: ignore[no-untyped-def]
        return self.traded


class _RecordingCRM:
    def __init__(self) -> None:
        self.signals: list = []

    def evaluate(self, signal, ctx):  # type: ignore[no-untyped-def]
        self.signals.append(signal)
        return _APPROVE_ALL_CRM.evaluate(signal, ctx)
