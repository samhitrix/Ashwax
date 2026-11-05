"""Flattrade market data via GetQuotes / TPSeries / EODChartData (findings A3, A6, C-7, C-8).

Every HTTP call goes through FlattradeClient with an injected fake transport; the
scrip master comes from the inline CSV fixtures. No network, no credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from src.brokers.adapters.flattrade import adapter as adapter_mod
from src.brokers.adapters.flattrade.adapter import FlattradeAdapter
from src.brokers.adapters.flattrade.auth import FlattradeAuth
from src.brokers.adapters.flattrade.client import (
    FlattradeClient,
    FlattradeError,
    FlattradeSessionExpired,
)
from src.brokers.adapters.flattrade.normalizer import normalize_tick
from src.brokers.adapters.flattrade.symbols import FlattradeSymbols
from src.brokers.base import MarketDepth, OptionQuote, Tick

from tests.unit.brokers.fixtures.flattrade_scrip_samples import FILES

_IST = ZoneInfo("Asia/Kolkata")
_NOW = datetime(2026, 10, 15, 15, 0, tzinfo=_IST)

Handler = Any  # (endpoint, jdata) -> reply (or raises)


class _Limiter:
    def __init__(self) -> None:
        self.categories: list[str] = []

    async def acquire(self, category: str = "general", tokens: float = 1.0) -> float:
        self.categories.append(category)
        return 0.0


class _Transport:
    """Fake transport: decodes jData and hands (endpoint, payload) to a handler."""

    def __init__(self, handler: Handler, delay: float = 0.0) -> None:
        self.handler = handler
        self.delay = delay
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
        endpoint = url.rsplit("/", 1)[-1]
        payload = json.loads(body["jData"])
        self.calls.append((endpoint, payload))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.handler(endpoint, payload)
        finally:
            self.in_flight -= 1


async def _fetch(url: str) -> str:
    return FILES[url.rsplit("/", 1)[-1]]


def _adapter(
    handler: Handler, *, delay: float = 0.0, max_concurrent_quotes: int = 4
) -> tuple[FlattradeAdapter, _Transport, _Limiter]:
    transport = _Transport(handler, delay)
    limiter = _Limiter()
    client = FlattradeClient(
        transport=transport, limiter=limiter, user_id="FZ00000", access_token="tok"
    )
    auth = FlattradeAuth("key", "FZ00000", client=client)
    adapter = FlattradeAdapter(
        auth,
        symbols=FlattradeSymbols(fetch_text=_fetch),
        max_concurrent_quotes=max_concurrent_quotes,
    )
    return adapter, transport, limiter


def _quote(token: str, **fields: Any) -> dict[str, Any]:
    base = {"stat": "Ok", "exch": "NSE", "token": token, "tsym": "X", "lp": "100.50", "ti": "0.05"}
    base.update(fields)
    return base


def _quotes_by_token(prices: dict[str, Any]) -> Handler:
    def handler(endpoint: str, p: dict[str, Any]) -> Any:
        assert endpoint == "GetQuotes"
        reply = prices[p["token"]]
        return reply if isinstance(reply, dict) else _quote(p["token"], lp=reply)

    return handler


@pytest.fixture(autouse=True)
def _frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_mod, "_now", lambda: _NOW)


# ── instruments ────────────────────────────────────────────────────────────────


async def test_get_instruments_nse_count_and_bare_equity_symbols() -> None:
    adapter, transport, _ = _adapter(lambda *_: {})
    insts = await adapter.get_instruments("NSE")
    assert len(insts) == 7
    by_token = {i.instrument_token: i for i in insts}
    assert by_token["2885"].symbol == "RELIANCE"  # -EQ exposed by bare name
    assert by_token["2885"].instrument_type == "EQ"
    assert by_token["90001"].symbol == "SOMEBE-BE"  # BE keeps its tradingsymbol
    assert by_token["26000"].symbol == "Nifty 50"
    assert transport.calls == []  # scrip master is not an API call


async def test_get_instruments_nfo_rows() -> None:
    adapter, _, _ = _adapter(lambda *_: {})
    insts = await adapter.get_instruments("NFO")
    assert len(insts) == 4
    pe = next(i for i in insts if i.symbol == "NIFTY29DEC26P21050")
    assert (pe.instrument_type, pe.strike, pe.lot_size, pe.name) == ("PE", 21050.0, 65, "NIFTY")


async def test_get_instruments_unsupported_exchange_is_capability_gap() -> None:
    adapter, _, _ = _adapter(lambda *_: {})
    with pytest.raises(NotImplementedError):
        await adapter.get_instruments("MCX")


async def test_get_instruments_download_failure_is_loud() -> None:
    async def broken(url: str) -> str:
        raise OSError("s3 down")

    client = FlattradeClient(transport=_Transport(lambda *_: {}), limiter=_Limiter())
    adapter = FlattradeAdapter(
        FlattradeAuth("k", "FZ00000", client=client), symbols=FlattradeSymbols(fetch_text=broken)
    )
    assert await adapter.load_instrument_tokens("NSE") == 0  # soft
    with pytest.raises(FlattradeError, match="scrip master"):
        await adapter.get_instruments("NSE")  # loud


async def test_load_instrument_tokens_returns_count() -> None:
    adapter, _, _ = _adapter(lambda *_: {})
    assert await adapter.load_instrument_tokens() > 0


# ── get_ltp ────────────────────────────────────────────────────────────────────


async def test_get_ltp_one_getquotes_per_symbol_keyed_by_request() -> None:
    adapter, transport, limiter = _adapter(_quotes_by_token({"2885": "1400.25", "26000": "24000"}))
    out = await adapter.get_ltp(["NSE:RELIANCE", "NIFTY 50"])
    assert out == {"NSE:RELIANCE": 1400.25, "NIFTY 50": 24000.0}
    assert sorted((e, p["exch"], p["token"]) for e, p in transport.calls) == [
        ("GetQuotes", "NSE", "26000"),
        ("GetQuotes", "NSE", "2885"),
    ]
    assert limiter.categories == ["general", "general"]


async def test_get_ltp_same_token_twice_makes_one_call() -> None:
    adapter, transport, _ = _adapter(_quotes_by_token({"2885": "1400"}))
    out = await adapter.get_ltp(["RELIANCE", "NSE:RELIANCE-EQ"])
    assert out == {"RELIANCE": 1400.0, "NSE:RELIANCE-EQ": 1400.0}
    assert len(transport.calls) == 1


async def test_get_ltp_unresolvable_raises_keyerror_before_any_request() -> None:
    adapter, transport, _ = _adapter(_quotes_by_token({"2885": "1"}))
    with pytest.raises(KeyError):
        await adapter.get_ltp(["RELIANCE", "NOPE"])
    assert transport.calls == []


async def test_get_ltp_empty_makes_no_call() -> None:
    adapter, transport, _ = _adapter(_quotes_by_token({}))
    assert await adapter.get_ltp([]) == {}
    assert transport.calls == []


async def test_get_ltp_symbol_with_broker_error_is_omitted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    replies = {"2885": "1400", "3045": {"stat": "Not_Ok", "emsg": "Invalid Input : bad token"}}
    adapter, _, _ = _adapter(_quotes_by_token(replies))
    with caplog.at_level(logging.WARNING):
        out = await adapter.get_ltp(["RELIANCE", "SBIN"])
    assert out == {"RELIANCE": 1400.0}
    assert "flattrade_quote_failed" in caplog.text


async def test_get_ltp_missing_lp_is_zero_like_angel() -> None:
    reply = {"stat": "Ok", "exch": "NSE", "token": "2885"}
    adapter, _, _ = _adapter(_quotes_by_token({"2885": reply}))
    assert await adapter.get_ltp(["RELIANCE"]) == {"RELIANCE": 0.0}


async def test_get_ltp_session_expired_propagates() -> None:
    expired = {"stat": "Not_Ok", "emsg": "Session Expired : Invalid Session Key"}
    adapter, _, _ = _adapter(_quotes_by_token({"2885": expired, "3045": "1"}))
    with pytest.raises(FlattradeSessionExpired):
        await adapter.get_ltp(["RELIANCE", "SBIN"])


async def test_get_ltp_concurrency_is_bounded() -> None:
    prices = {t: "1" for t in ("2885", "3045", "26000", "26009", "26017", "26037")}
    adapter, transport, _ = _adapter(_quotes_by_token(prices), delay=0.01, max_concurrent_quotes=2)
    out = await adapter.get_ltp(
        ["RELIANCE", "SBIN", "NIFTY 50", "NIFTY BANK", "INDIA VIX", "Nifty Fin"]
    )
    assert len(out) == 6
    assert transport.max_in_flight == 2


# ── get_depth ──────────────────────────────────────────────────────────────────


async def test_get_depth_maps_five_levels() -> None:
    fields: dict[str, Any] = {"lp": "1400.10", "ltt": "15-10-2026 14:59:58"}
    for i in range(1, 6):
        fields[f"bp{i}"] = f"{1400 - i}.00"
        fields[f"bq{i}"] = str(10 * i)
        fields[f"sp{i}"] = f"{1400 + i}.00"
        fields[f"sq{i}"] = str(20 * i)
    adapter, transport, _ = _adapter(_quotes_by_token({"2885": _quote("2885", **fields)}))
    d = await adapter.get_depth("NSE:RELIANCE")
    assert isinstance(d, MarketDepth)
    assert d.symbol == "NSE:RELIANCE"
    assert [(b.price, b.quantity) for b in d.bids] == [
        (1399.0, 10),
        (1398.0, 20),
        (1397.0, 30),
        (1396.0, 40),
        (1395.0, 50),
    ]
    assert d.asks[0].price == 1401.0 and d.asks[4].quantity == 100
    assert d.bids[0].orders == 0  # order counts are not in GetQuotes
    assert d.ltp == 1400.10
    assert d.timestamp == datetime(2026, 10, 15, 14, 59, 58, tzinfo=_IST)
    assert transport.calls[0][1]["token"] == "2885"


async def test_get_depth_broker_error_raises() -> None:
    reply = {"stat": "Not_Ok", "emsg": "Invalid Input"}
    adapter, _, _ = _adapter(_quotes_by_token({"2885": reply}))
    with pytest.raises(FlattradeError):
        await adapter.get_depth("RELIANCE")


# ── get_option_quotes ──────────────────────────────────────────────────────────


async def test_get_option_quotes_maps_fields_and_defaults_to_nfo() -> None:
    reply = _quote("62354", exch="NFO", lp="85.5", bp1="85.0", sp1="86.0", v="12345", oi="67890")
    adapter, transport, _ = _adapter(_quotes_by_token({"62354": reply}))
    out = await adapter.get_option_quotes(["NIFTY29DEC26P21050"])
    q = out["NIFTY29DEC26P21050"]
    assert isinstance(q, OptionQuote)
    assert (q.ltp, q.bid, q.ask, q.volume, q.oi, q.prev_oi) == (85.5, 85.0, 86.0, 12345, 67890, 0)
    assert transport.calls[0][1]["exch"] == "NFO"


async def test_get_option_quotes_missing_oi_is_zero_and_prefixed_keys_kept() -> None:
    reply = _quote("62355", exch="NFO", lp="10", bp1="9.9", sp1="10.1", v="5")
    adapter, _, _ = _adapter(_quotes_by_token({"62355": reply}))
    out = await adapter.get_option_quotes(["NFO:NIFTY29DEC26C21050"])
    assert out["NFO:NIFTY29DEC26C21050"].oi == 0


async def test_get_option_quotes_failed_leg_omitted() -> None:
    replies = {"62354": {"stat": "Not_Ok", "emsg": "Invalid Input"}, "62355": "10"}
    adapter, _, _ = _adapter(_quotes_by_token(replies))
    out = await adapter.get_option_quotes(["NIFTY29DEC26P21050", "NIFTY29DEC26C21050"])
    assert list(out) == ["NIFTY29DEC26C21050"]


async def test_get_option_quotes_large_chain_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    header = FILES["Nfo_Index_Derivatives.csv"].splitlines()[0]
    strikes = range(20000, 20000 + 41 * 50, 50)  # 41 distinct legs > 40
    nfo = "\n".join(
        [header]
        + [
            f"NFO,{80000 + k},65,NIFTY,NIFTY29DEC26C{k},OPTIDX,29-DEC-2026,{k}.00,CE"
            for k in strikes
        ]
    )
    files = {**FILES, "Nfo_Index_Derivatives.csv": nfo, "Nfo_Equity_Derivatives.csv": header}

    async def fetch(url: str) -> str:
        return files[url.rsplit("/", 1)[-1]]

    adapter, _, _ = _adapter(lambda e, p: _quote(p["token"]))
    adapter._symbols = FlattradeSymbols(fetch_text=fetch)
    legs = [f"NIFTY29DEC26C{k}" for k in strikes]
    with caplog.at_level(logging.WARNING):
        await adapter.get_option_quotes(legs)
        await adapter.get_option_quotes(legs)
    assert caplog.text.count("flattrade_option_chain_slow") == 1


async def test_get_option_quotes_empty() -> None:
    adapter, transport, _ = _adapter(_quotes_by_token({}))
    assert await adapter.get_option_quotes([]) == {}
    assert transport.calls == []


# ── get_ohlcv: TPSeries ────────────────────────────────────────────────────────


def _tp_row(ts: str, o: float, vol: int = 100) -> dict[str, Any]:
    return {
        "stat": "Ok",
        "time": ts,
        "into": f"{o:.2f}",
        "inth": f"{o + 2:.2f}",
        "intl": f"{o - 1:.2f}",
        "intc": f"{o + 1:.2f}",
        "intv": str(vol),
        "intvwap": "0",
        "intoi": "0",
        "v": "0",
        "oi": "0",
    }


@pytest.mark.parametrize(
    ("interval", "intrv"),
    [
        ("minute", "1"),
        ("3minute", "3"),
        ("5minute", "5"),
        ("10minute", "10"),
        ("15minute", "15"),
        ("30minute", "30"),
        ("60minute", "60"),
        ("120minute", "120"),
    ],
)
async def test_tpseries_interval_map(interval: str, intrv: str) -> None:
    adapter, transport, _ = _adapter(lambda e, p: [_tp_row("15-10-2026 09:15:00", 100)])
    await adapter.get_ohlcv("RELIANCE", interval, 1)
    endpoint, payload = transport.calls[0]
    assert endpoint == "TPSeries"
    assert payload["intrv"] == intrv
    assert (payload["exch"], payload["token"]) == ("NSE", "2885")


async def test_tpseries_newest_first_parsed_ascending_ist() -> None:
    rows = [
        _tp_row("15-10-2026 09:25:00", 102),
        _tp_row("15-10-2026 09:20:00", 101, vol=7),
        _tp_row("15-10-2026 09:15:00", 100),
    ]
    adapter, _, _ = _adapter(lambda e, p: rows)
    df = await adapter.get_ohlcv("RELIANCE", "5minute", 1)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert str(df["date"].dtype) == "datetime64[ns, Asia/Kolkata]"
    assert list(df["open"]) == [100.0, 101.0, 102.0]
    assert df["date"].iloc[0] == pd.Timestamp("2026-10-15 09:15:00", tz=_IST)
    assert df["volume"].iloc[1] == 7
    assert (df["high"].iloc[0], df["low"].iloc[0], df["close"].iloc[0]) == (102.0, 99.0, 101.0)


async def test_tpseries_chunks_into_30_day_windows_and_dedups() -> None:
    def handler(e: str, p: dict[str, Any]) -> Any:
        # every window returns the same boundary candle -> must de-duplicate
        return [_tp_row("15-10-2026 09:15:00", 100)]

    adapter, transport, _ = _adapter(handler)
    df = await adapter.get_ohlcv("RELIANCE", "15minute", 75)
    windows = [(int(p["st"]), int(p["et"])) for _, p in transport.calls]
    assert len(windows) == 3
    assert all(isinstance(p["st"], str) and isinstance(p["et"], str) for _, p in transport.calls)
    assert windows[0][0] == int((_NOW - timedelta(days=75)).timestamp())
    assert windows[-1][1] == int(_NOW.timestamp())
    for (_, e), (s2, _) in zip(windows, windows[1:], strict=False):
        assert s2 == e  # contiguous
    assert all(e - s <= 30 * 86400 for s, e in windows)
    assert len(df) == 1


async def test_tpseries_no_data_window_is_skipped() -> None:
    calls = {"n": 0}

    def handler(e: str, p: dict[str, Any]) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"stat": "Not_Ok", "emsg": "No Data : "}
        return [_tp_row("15-10-2026 09:15:00", 100)]

    adapter, _, _ = _adapter(handler)
    df = await adapter.get_ohlcv("RELIANCE", "5minute", 45)
    assert len(df) == 1


async def test_tpseries_all_no_data_returns_typed_empty_frame() -> None:
    adapter, _, _ = _adapter(lambda e, p: {"stat": "Not_Ok", "emsg": "No Data : "})
    df = await adapter.get_ohlcv("RELIANCE", "5minute", 5)
    assert df.empty
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert str(df["date"].dtype) == "datetime64[ns, Asia/Kolkata]"


async def test_tpseries_other_error_raises_no_partial_history() -> None:
    adapter, _, _ = _adapter(lambda e, p: {"stat": "Not_Ok", "emsg": "Server Timeout :  "})
    with pytest.raises(FlattradeError, match="Server Timeout"):
        await adapter.get_ohlcv("RELIANCE", "5minute", 5)


async def test_tpseries_error_occurred_no_data_variant_is_loud() -> None:
    adapter, _, _ = _adapter(
        lambda e, p: {"stat": "Not_Ok", "emsg": 'Error Occurred : 5 "no data"'}
    )
    with pytest.raises(FlattradeError):
        await adapter.get_ohlcv("RELIANCE", "5minute", 5)


async def test_empty_position_book_error_occurred_no_data_is_an_empty_list() -> None:
    adapter, _, _ = _adapter(
        lambda e, p: {"stat": "Not_Ok", "emsg": 'Error Occurred : 5 "no data"'}
    )
    assert await adapter.get_positions() == []


async def test_eod_day_label_format_parses() -> None:
    from src.brokers.adapters.flattrade.normalizer import parse_time

    assert parse_time("15-OCT-2026") == datetime(2026, 10, 15, tzinfo=_IST)


async def test_tpseries_object_instead_of_array_raises() -> None:
    adapter, _, _ = _adapter(lambda e, p: {"request_time": "x"})  # no stat, not a list
    with pytest.raises(FlattradeError, match="TPSeries"):
        await adapter.get_ohlcv("RELIANCE", "5minute", 5)


async def test_tpseries_epoch_time_form() -> None:
    epoch = int(datetime(2026, 10, 15, 9, 15, tzinfo=_IST).timestamp())
    row = _tp_row(str(epoch), 100)
    adapter, _, _ = _adapter(lambda e, p: [row])
    df = await adapter.get_ohlcv("RELIANCE", "5minute", 1)
    assert df["date"].iloc[0] == pd.Timestamp("2026-10-15 09:15:00", tz=_IST)


async def test_tpseries_unparseable_row_raises() -> None:
    row = _tp_row("not a time", 100)
    adapter, _, _ = _adapter(lambda e, p: [row])
    with pytest.raises(ValueError):
        await adapter.get_ohlcv("RELIANCE", "5minute", 1)


async def test_unknown_interval_rejected_before_any_call() -> None:
    adapter, transport, _ = _adapter(lambda e, p: [])
    with pytest.raises(ValueError, match="interval"):
        await adapter.get_ohlcv("RELIANCE", "week", 10)
    assert transport.calls == []


async def test_ohlcv_unresolvable_symbol_keyerror() -> None:
    adapter, transport, _ = _adapter(lambda e, p: [])
    with pytest.raises(KeyError):
        await adapter.get_ohlcv("NOPE", "day", 10)
    assert transport.calls == []


# ── get_ohlcv: EODChartData ────────────────────────────────────────────────────


def _eod(ts: str, o: float) -> dict[str, Any]:
    return {
        "time": ts,
        "into": str(o),
        "inth": str(o + 5),
        "intl": str(o - 5),
        "intc": str(o + 1),
        "ssboe": "0",
        "intv": "1000",
    }


async def test_eod_request_shape_and_string_elements() -> None:
    rows = [
        json.dumps(_eod("14-10-2026 00:00:00", 1410)),  # JSON-string elements (doc sample)
        json.dumps(_eod("13-10-2026 00:00:00", 1400)),
    ]
    adapter, transport, _ = _adapter(lambda e, p: rows)
    df = await adapter.get_ohlcv("NSE:RELIANCE", "day", 30)
    endpoint, payload = transport.calls[0]
    assert endpoint == "EODChartData"
    assert payload["sym"] == "NSE:RELIANCE-EQ"
    assert payload["from"] == str(int((_NOW - timedelta(days=30)).timestamp()))
    assert payload["to"] == str(int(_NOW.timestamp()))
    assert list(df["open"]) == [1400.0, 1410.0]
    assert df["date"].iloc[0] == pd.Timestamp("2026-10-13", tz=_IST)


async def test_eod_object_elements_and_index_sym() -> None:
    rows = [_eod("13-10-2026 00:00:00", 24000), _eod("13-10-2026 00:00:00", 24000)]
    adapter, transport, _ = _adapter(lambda e, p: rows)
    df = await adapter.get_ohlcv("NIFTY 50", "day", 10)
    assert transport.calls[0][1]["sym"] == "NSE:Nifty 50"
    assert len(df) == 1  # de-duplicated


async def test_eod_no_data_is_empty_frame() -> None:
    adapter, _, _ = _adapter(lambda e, p: {"stat": "Not_Ok", "emsg": "No Data : "})
    df = await adapter.get_ohlcv("RELIANCE", "day", 10)
    assert df.empty and str(df["date"].dtype) == "datetime64[ns, Asia/Kolkata]"


async def test_eod_session_expired_raises() -> None:
    adapter, _, _ = _adapter(
        lambda e, p: {"stat": "Not_Ok", "emsg": "Session Expired : Invalid Session Key"}
    )
    with pytest.raises(FlattradeSessionExpired):
        await adapter.get_ohlcv("RELIANCE", "day", 10)


# ── ticks / normalizers ────────────────────────────────────────────────────────


async def test_subscribe_and_unsubscribe_record_tokens() -> None:
    adapter, transport, _ = _adapter(lambda *_: {})
    await adapter.subscribe_ticks(["NSE|2885", "NSE|26000"])
    await adapter.unsubscribe_ticks(["NSE|26000"])
    assert adapter._subscribed_tokens == {"NSE|2885"}
    assert transport.calls == []


async def test_subscribe_with_callback_warns_that_no_feed_is_wired(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter, _, _ = _adapter(lambda *_: {})
    with caplog.at_level(logging.WARNING):
        await adapter.subscribe_ticks(["NSE:RELIANCE"], lambda tick: None)  # tick-ingester form
    assert "flattrade_ticks_not_wired" in caplog.text


def test_normalize_tick_from_touchline_message() -> None:
    raw = {
        "t": "tk",
        "e": "NSE",
        "tk": "2885",
        "ts": "RELIANCE-EQ",
        "ti": "0.05",
        "ls": "1",
        "pp": "2",
        "lp": "1400.55",
        "pc": "0.5",
        "v": "123456",
        "o": "1390.00",
        "h": "1405.00",
        "l": "1388.00",
        "c": "1393.00",
        "ap": "1398.10",
        "oi": "0",
        "bq1": "50",
        "bp1": "1400.50",
        "sq1": "30",
        "sp1": "1400.60",
        "ft": str(int(datetime(2026, 10, 15, 10, 0, tzinfo=_IST).timestamp())),
    }
    tick = normalize_tick(raw)
    assert isinstance(tick, Tick)
    assert (tick.symbol, tick.instrument_token) == ("RELIANCE-EQ", "2885")
    assert (tick.ltp, tick.bid, tick.ask, tick.volume) == (1400.55, 1400.50, 1400.60, 123456)
    assert tick.exchange_ts == datetime(2026, 10, 15, 10, 0, tzinfo=_IST)
    adapter, _, _ = _adapter(lambda *_: {})
    assert adapter.normalize_tick(raw).ltp == 1400.55  # adapter delegates to the normalizer


def test_normalize_tick_without_ltp_raises() -> None:
    # a partial `tf` update must be merged onto the `tk` snapshot first
    with pytest.raises(ValueError, match="lp"):
        normalize_tick({"t": "tf", "e": "NSE", "tk": "2885", "v": "10"})


async def test_get_order_margin_still_not_implemented() -> None:
    adapter, _, _ = _adapter(lambda *_: {})
    with pytest.raises(NotImplementedError):
        await adapter.get_order_margin([])


# ── fix round 1 ────────────────────────────────────────────────────────────────


@pytest.fixture
def _fresh_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_mod, "_warned_bar_alignment", False)


async def test_get_ltp_transport_error_propagates() -> None:
    from src.brokers.adapters.flattrade.client import FlattradeTransportError

    def handler(e: str, p: dict[str, Any]) -> Any:
        if p["token"] == "3045":
            raise FlattradeTransportError("GetQuotes", ambiguous=True, cause=TimeoutError())
        return _quote(p["token"])

    adapter, _, _ = _adapter(handler)
    with pytest.raises(FlattradeTransportError):
        await adapter.get_ltp(["RELIANCE", "SBIN"])


async def test_daily_bars_stamped_at_midnight_ist_and_deduplicated() -> None:
    ssboe = str(int(datetime(2026, 10, 14, 15, 30, tzinfo=_IST).timestamp()))
    rows = [
        json.dumps(_eod("15-10-2026 15:30:00", 1410)),
        {**_eod("", 1400), "ssboe": ssboe},
        _eod("15-10-2026 00:00:00", 1411),  # same trading day, other stamp
    ]
    adapter, _, _ = _adapter(lambda e, p: rows)
    df = await adapter.get_ohlcv("RELIANCE", "day", 5)
    assert list(df["date"]) == [
        pd.Timestamp("2026-10-14", tz=_IST),
        pd.Timestamp("2026-10-15", tz=_IST),
    ]
    assert df["open"].iloc[1] == 1411.0  # last wins after normalising


async def test_volume_is_int64() -> None:
    adapter, _, _ = _adapter(lambda e, p: [_tp_row("15-10-2026 09:15:00", 100, vol=7)])
    df = await adapter.get_ohlcv("RELIANCE", "5minute", 1)
    assert str(df["volume"].dtype) == "int64"
    empty, _, _ = _adapter(lambda e, p: {"stat": "Not_Ok", "emsg": "No Data : "})
    assert str((await empty.get_ohlcv("RELIANCE", "5minute", 1))["volume"].dtype) == "int64"


async def test_eod_truncation_warns_once_per_call(caplog: pytest.LogCaptureFixture) -> None:
    rows = [_eod("13-10-2026 00:00:00", 1), _eod("14-10-2026 00:00:00", 2)]
    adapter, _, _ = _adapter(lambda e, p: rows)
    with caplog.at_level(logging.WARNING):
        await adapter.get_ohlcv("RELIANCE", "day", 400)
    assert caplog.text.count("flattrade_eod_possibly_truncated") == 1
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await adapter.get_ohlcv("RELIANCE", "day", 4)  # starts on time: no warning
    assert "flattrade_eod_possibly_truncated" not in caplog.text


@pytest.mark.usefixtures("_fresh_warnings")
async def test_misaligned_intraday_stamps_warn_once_per_process(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows = [_tp_row("15-10-2026 09:17:00", 100), _tp_row("15-10-2026 09:22:30", 101)]
    adapter, _, _ = _adapter(lambda e, p: rows)
    with caplog.at_level(logging.WARNING):
        await adapter.get_ohlcv("RELIANCE", "5minute", 1)
        await adapter.get_ohlcv("RELIANCE", "5minute", 1)
    assert caplog.text.count("flattrade_bar_stamps_misaligned") == 1
    assert adapter_mod.BAR_LABEL_CONVENTION == "unverified"


@pytest.mark.usefixtures("_fresh_warnings")
@pytest.mark.parametrize(
    ("interval", "stamps"),
    [
        ("5minute", ["09:15:00", "09:20:00"]),
        ("10minute", ["09:15:00", "09:25:00"]),  # session-anchored, not clock-anchored
        ("30minute", ["09:15:00", "09:45:00"]),
        ("60minute", ["09:15:00", "10:15:00"]),
    ],
)
async def test_session_aligned_stamps_do_not_warn(
    interval: str, stamps: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    rows = [_tp_row(f"15-10-2026 {t}", 100 + i) for i, t in enumerate(stamps)]
    adapter, _, _ = _adapter(lambda e, p: rows)
    with caplog.at_level(logging.WARNING):
        await adapter.get_ohlcv("RELIANCE", interval, 1)
    assert "flattrade_bar_stamps_misaligned" not in caplog.text


async def test_missing_oi_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    reply = _quote("62355", exch="NFO", lp="10")
    adapter, _, _ = _adapter(_quotes_by_token({"62355": reply}))
    with caplog.at_level(logging.WARNING):
        await adapter.get_option_quotes(["NIFTY29DEC26C21050"])
        await adapter.get_option_quotes(["NIFTY29DEC26C21050"])
    assert caplog.text.count("flattrade_quote_without_oi") == 1
