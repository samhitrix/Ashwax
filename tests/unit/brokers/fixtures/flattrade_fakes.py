"""Shared fakes for the Flattrade order/account tests (no network, no credentials).

`Router` answers each endpoint from a dict of callables or canned replies, records
every call, and can raise an exception per endpoint. Payload samples are built from
the field lists in the Phase 0 findings (A3); values are SYNTHETIC.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.brokers.adapters.flattrade.adapter import FlattradeAdapter
from src.brokers.adapters.flattrade.auth import FlattradeAuth
from src.brokers.adapters.flattrade.client import FlattradeClient
from src.brokers.adapters.flattrade.symbols import FlattradeSymbols

from tests.unit.brokers.fixtures.flattrade_scrip_samples import FILES

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 15, 11, 0, tzinfo=IST)
NOW_EPOCH = str(int(NOW.timestamp()))

USER_DETAILS: dict[str, Any] = {
    "stat": "Ok",
    "actid": "FZ00000",
    "exarr": ["NSE", "BSE", "NFO"],
    "orarr": ["LMT", "SL-LMT", "DS", "2L", "3L", "4L"],
    "prarr": [
        {"prd": "C", "s_prdt_ali": "CNC", "exch": ["NSE", "BSE"]},
        {"prd": "M", "s_prdt_ali": "NRML", "exch": ["NSE", "BSE", "NFO"]},
        {"prd": "I", "s_prdt_ali": "MIS", "exch": ["NSE", "BSE", "NFO"]},
    ],
}


class Limiter:
    def __init__(self) -> None:
        self.categories: list[str] = []

    async def acquire(self, category: str = "general", tokens: float = 1.0) -> float:
        self.categories.append(category)
        return 0.0


class Router:
    """Fake transport. `replies[endpoint]` is a reply, a callable(payload) -> reply,
    or an exception instance (raised). Lists of replies are served in order."""

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.replies: dict[str, Any] = {"UserDetails": USER_DETAILS, **(replies or {})}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def payloads(self, endpoint: str) -> list[dict[str, Any]]:
        return [p for e, p in self.calls if e == endpoint]

    async def __call__(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
        endpoint = url.rsplit("/", 1)[-1]
        payload = json.loads(body["jData"])
        self.calls.append((endpoint, payload))
        if endpoint not in self.replies:
            raise AssertionError(f"unexpected endpoint {endpoint}")
        reply = self.replies[endpoint]
        if isinstance(reply, list) and reply and isinstance(reply[0], _Seq):
            reply = reply.pop(0).value
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return reply(payload)
        return reply


class _Seq:
    def __init__(self, value: Any) -> None:
        self.value = value


def seq(*values: Any) -> list[_Seq]:
    """Serve these replies one per call, in order."""
    return [_Seq(v) for v in values]


async def _fetch(url: str) -> str:
    return FILES[url.rsplit("/", 1)[-1]]


def make_adapter(
    replies: dict[str, Any] | None = None, *, band: float = 0.005
) -> tuple[FlattradeAdapter, Router, Limiter]:
    router = Router(replies)
    limiter = Limiter()
    client = FlattradeClient(
        transport=router, limiter=limiter, user_id="FZ00000", access_token="tok"
    )
    auth = FlattradeAuth("key", "FZ00000", client=client)
    adapter = FlattradeAdapter(
        auth, symbols=FlattradeSymbols(fetch_text=_fetch), protection_band=band
    )
    return adapter, router, limiter


def order_row(**fields: Any) -> dict[str, Any]:
    """An OrderBook / SingleOrdHist row (findings A3 field list), SYNTHETIC values."""
    row: dict[str, Any] = {
        "stat": "Ok",
        "norenordno": "26101500000123",
        "exch": "NSE",
        "tsym": "RELIANCE-EQ",
        "trantype": "B",
        "prctyp": "LMT",
        "prd": "I",
        "qty": "10",
        "prc": "2500.00",
        "trgprc": "0.00",
        "fillshares": "0",
        "avgprc": "0.00",
        "status": "OPEN",
        "rejreason": "",
        "remarks": "T1",
        "exchordid": "1100000001",
        "ret": "DAY",
        "token": "2885",
        "ls": "1",
        "ti": "0.05",
        "pp": "2",
        "ordenttm": NOW_EPOCH,
    }
    row.update(fields)
    return row


QuoteFn = Callable[[dict[str, Any]], dict[str, Any]]


def quote(lp: str, ti: str = "0.05", **fields: Any) -> dict[str, Any]:
    reply = {"stat": "Ok", "exch": "NSE", "token": "2885", "tsym": "RELIANCE-EQ", "lp": lp}
    reply["ti"] = ti
    reply.update(fields)
    return reply
