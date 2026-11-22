"""Flattrade scrip-master parsing and neutral symbol mapping (findings A5, C-5)."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pytest
from src.brokers.adapters.flattrade import symbols as sym_mod
from src.brokers.adapters.flattrade.symbols import (
    DEFAULT_TICK_SIZE,
    SCRIP_MASTER_BASE_URL,
    FlattradeSymbols,
    build_token_table,
    parse_scrip_master,
    to_instrument,
)

from tests.unit.brokers.fixtures.flattrade_scrip_samples import FILES, NFO_INDEX_CSV, NSE_EQUITY_CSV


class _Fetch:
    """Injectable fetch: serves the fixture CSVs by file name and counts calls."""

    def __init__(self, files: dict[str, str] | None = None, fail: set[str] | None = None) -> None:
        self.files = FILES if files is None else files
        self.fail = fail or set()
        self.urls: list[str] = []

    async def __call__(self, url: str) -> str:
        self.urls.append(url)
        name = url.rsplit("/", 1)[-1]
        if name in self.fail:
            raise OSError(f"download failed: {name}")
        return self.files[name]


def _symbols(fetch: _Fetch | None = None) -> FlattradeSymbols:
    return FlattradeSymbols(fetch_text=fetch or _Fetch())


# ── parsing ────────────────────────────────────────────────────────────────────


def test_parse_reads_documented_header_and_short_rows() -> None:
    rows = parse_scrip_master(NSE_EQUITY_CSV)
    by_tsym = {r.tradingsymbol: r for r in rows}
    assert by_tsym["RELIANCE-EQ"].token == "2885"
    assert by_tsym["RELIANCE-EQ"].symbol == "RELIANCE"
    assert by_tsym["Nifty Bank"].instrument == "INDEX"  # 6-column row still parses
    assert by_tsym["Nifty Bank"].expiry == ""


def test_parse_rejects_unknown_header() -> None:
    with pytest.raises(ValueError, match="header"):
        parse_scrip_master("Foo,Bar\n1,2\n")


def test_parse_tolerates_utf8_bom() -> None:
    rows = parse_scrip_master("﻿" + NSE_EQUITY_CSV)
    assert rows[0].exchange == "NSE"


def test_nfo_option_row_to_instrument() -> None:
    row = next(
        r for r in parse_scrip_master(NFO_INDEX_CSV) if r.tradingsymbol == "NIFTY29DEC26P21050"
    )
    inst = to_instrument(row)
    assert inst.symbol == "NIFTY29DEC26P21050"  # used as given
    assert inst.exchange == "NFO"
    assert inst.instrument_token == "62354"
    assert inst.instrument_type == "PE"
    assert inst.lot_size == 65
    assert inst.expiry == datetime(2026, 12, 29)
    assert inst.strike == 21050.0  # rupees, no x100 scaling
    assert inst.name == "NIFTY"
    assert inst.tick_size == DEFAULT_TICK_SIZE == 0.05


def test_nfo_future_row_negative_strike_is_none() -> None:
    row = next(r for r in parse_scrip_master(NFO_INDEX_CSV) if r.tradingsymbol == "NIFTY27OCT26F")
    inst = to_instrument(row)
    assert inst.instrument_type == "FUT"
    assert inst.strike is None
    assert inst.expiry == datetime(2026, 10, 27)


def test_equity_and_index_rows_have_no_strike_or_expiry() -> None:
    rows = {r.tradingsymbol: r for r in parse_scrip_master(NSE_EQUITY_CSV)}
    eq = to_instrument(rows["RELIANCE-EQ"])
    idx = to_instrument(rows["Nifty 50"])
    assert (eq.instrument_type, eq.strike, eq.expiry, eq.lot_size) == ("EQ", None, None, 1)
    assert (idx.instrument_type, idx.strike, idx.expiry) == ("INDEX", None, None)


# ── token table / mapping rules ────────────────────────────────────────────────


def test_token_table_raw_keys_map_to_token_and_exchange() -> None:
    table = build_token_table(parse_scrip_master(NSE_EQUITY_CSV), "NSE")
    assert table["RELIANCE-EQ"] == ("2885", "NSE")
    assert table["Nifty 50"] == ("26000", "NSE")


@pytest.mark.parametrize(
    "symbol",
    ["RELIANCE", "NSE:RELIANCE", "RELIANCE-EQ", "NSE:RELIANCE-EQ", "reliance"],
)
async def test_equity_forms_resolve_to_eq_row(symbol: str) -> None:
    r = await _symbols().resolve(symbol)
    assert (r.token, r.exchange, r.tradingsymbol) == ("2885", "NSE", "RELIANCE-EQ")


async def test_be_series_is_not_aliased_to_bare_name() -> None:
    s = _symbols()
    assert (await s.resolve("SOMEBE-BE")).token == "90001"  # raw key works
    with pytest.raises(KeyError):
        await s.resolve("SOMEBE")


@pytest.mark.parametrize(
    ("symbol", "token", "tsym"),
    [
        ("NIFTY 50", "26000", "Nifty 50"),
        ("NSE:NIFTY 50", "26000", "Nifty 50"),
        ("Nifty 50", "26000", "Nifty 50"),
        ("nifty 50", "26000", "Nifty 50"),
        ("NIFTY", "26000", "Nifty 50"),
        ("NSE:NIFTY", "26000", "Nifty 50"),
        ("NIFTY BANK", "26009", "Nifty Bank"),
        ("NSE:NIFTY BANK", "26009", "Nifty Bank"),
        ("BANKNIFTY", "26009", "Nifty Bank"),
        ("INDIA VIX", "26017", "INDIAVIX"),
        ("NSE:INDIA VIX", "26017", "INDIAVIX"),
        ("INDIAVIX", "26017", "INDIAVIX"),
        ("india vix", "26017", "INDIAVIX"),
    ],
)
async def test_index_names_resolve(symbol: str, token: str, tsym: str) -> None:
    r = await _symbols().resolve(symbol)
    assert (r.token, r.exchange, r.tradingsymbol) == (token, "NSE", tsym)


def test_index_alias_never_replaces_an_equity_key(caplog: pytest.LogCaptureFixture) -> None:
    csv = "\n".join(
        [
            "Exchange,Token,Lotsize,Symbol,Tradingsymbol,Instrument,Expiry,Strike,Optiontype",
            "NSE,1,1,CLASH,CLASH-EQ,EQ,,0,",
            "NSE,2,1,CLASH,Clash Index,INDEX,,0,",
        ]
    )
    with caplog.at_level(logging.WARNING):
        table = build_token_table(parse_scrip_master(csv), "NSE")
    assert table["CLASH"] == ("1", "NSE")  # equity bare alias kept
    assert table["Clash Index"] == ("2", "NSE")
    assert "flattrade_index_alias_skipped" in caplog.text


async def test_nfo_symbols_resolve_with_prefix_or_default_exchange() -> None:
    s = _symbols()
    r = await s.resolve("NFO:NIFTY29DEC26P21050")
    assert (r.token, r.exchange) == ("62354", "NFO")
    r2 = await s.resolve("NIFTY27OCT26F", default_exchange="NFO")
    assert (r2.token, r2.exchange) == ("48704", "NFO")
    # NFO has no bare-underlying alias
    with pytest.raises(KeyError):
        await s.resolve("NFO:NIFTY")


async def test_unknown_symbol_raises_keyerror_naming_it() -> None:
    with pytest.raises(KeyError, match="NOPE"):
        await _symbols().resolve("NSE:NOPE")


# ── loading / caching ──────────────────────────────────────────────────────────


async def test_load_downloads_documented_urls_once_per_exchange() -> None:
    fetch = _Fetch()
    s = _symbols(fetch)
    n = await s.load("NSE")
    assert n == len(build_token_table(parse_scrip_master(NSE_EQUITY_CSV), "NSE"))
    assert await s.load("NSE") == n  # idempotent, no second download
    assert fetch.urls == [SCRIP_MASTER_BASE_URL + "NSE_Equity.csv"]
    await s.load("NFO")
    assert fetch.urls[1:] == [
        SCRIP_MASTER_BASE_URL + "Nfo_Index_Derivatives.csv",
        SCRIP_MASTER_BASE_URL + "Nfo_Equity_Derivatives.csv",
    ]
    assert SCRIP_MASTER_BASE_URL == "https://flattrade.s3.ap-south-1.amazonaws.com/scripmaster/"


async def test_per_exchange_caches_are_separate() -> None:
    s = _symbols()
    await s.load("NFO")
    with pytest.raises(KeyError):
        await s.resolve("NFO:RELIANCE-EQ")
    assert (await s.resolve("NFO:RELIANCE27OCT26C1400")).token == "70001"
    assert (await s.resolve("NSE:RELIANCE")).token == "2885"


async def test_failed_download_is_soft_then_loud(caplog: pytest.LogCaptureFixture) -> None:
    fetch = _Fetch(fail={"NSE_Equity.csv"})
    s = _symbols(fetch)
    with caplog.at_level(logging.WARNING):
        assert await s.load("NSE") == 0
    assert "flattrade_scrip_master_load_failed" in caplog.text
    with pytest.raises(KeyError):
        await s.resolve("RELIANCE")
    # retried later, once the file is available again
    fetch.fail.clear()
    assert (await s.resolve("RELIANCE")).token == "2885"


async def test_nfo_load_is_all_or_nothing() -> None:
    fetch = _Fetch(fail={"Nfo_Equity_Derivatives.csv"})
    s = _symbols(fetch)
    assert await s.load("NFO") == 0
    with pytest.raises(KeyError):
        await s.resolve("NFO:NIFTY29DEC26P21050")


async def test_malformed_csv_is_soft_failure() -> None:
    s = _symbols(_Fetch(files={"NSE_Equity.csv": "garbage,only\n1,2"}))
    assert await s.load("NSE") == 0


async def test_unsupported_exchange_loads_nothing(caplog: pytest.LogCaptureFixture) -> None:
    fetch = _Fetch()
    s = _symbols(fetch)
    with caplog.at_level(logging.WARNING):
        assert await s.load("BFO") == 0
    assert fetch.urls == []
    with pytest.raises(KeyError):
        await s.resolve("BFO:ANY")


async def test_instruments_returns_parsed_rows_per_exchange() -> None:
    s = _symbols()
    nse = await s.instruments("NSE")
    assert len(nse) == 7  # duplicate RELIANCE row collapsed
    nfo = await s.instruments("NFO")
    assert {i.symbol for i in nfo} == {
        "NIFTY29DEC26P21050",
        "NIFTY27OCT26F",
        "NIFTY29DEC26C21050",
        "RELIANCE27OCT26C1400",
    }


def test_default_fetch_uses_60s_timeout() -> None:
    assert sym_mod.DOWNLOAD_TIMEOUT_SECONDS == 60.0


# ── fix round 1: explicit index aliases, malformed rows ────────────────────────


@pytest.mark.parametrize(
    ("symbol", "token"),
    [
        ("NIFTY FIN SERVICE", "26037"),  # src/options/nfo_chain.py FINNIFTY spot name
        ("NSE:NIFTY FIN SERVICE", "26037"),
        ("nifty fin service", "26037"),
        ("FINNIFTY", "26037"),
        ("NIFTY 50", "26000"),
        ("NIFTY BANK", "26009"),
        ("INDIA VIX", "26017"),
    ],
)
async def test_codebase_index_vocabulary_resolves(symbol: str, token: str) -> None:
    assert (await _symbols().resolve(symbol)).token == token


def test_explicit_index_alias_needs_the_index_row() -> None:
    # alias keyed by token: a missing row (or a non-INDEX row) adds nothing
    csv = "\n".join(
        [
            "Exchange,Token,Lotsize,Symbol,Tradingsymbol,Instrument,Expiry,Strike,Optiontype",
            "NSE,26037,1,FINNIFTY,FINNIFTY-EQ,EQ,,0,",
        ]
    )
    table = build_token_table(parse_scrip_master(csv), "NSE")
    assert "NIFTY FIN SERVICE" not in table


async def test_malformed_row_is_skipped_counted_and_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.unit.brokers.fixtures.flattrade_scrip_samples import HEADER

    csv = "\n".join(
        [
            HEADER,
            "NSE,1,abc,BAD,BAD-EQ,EQ,,0,",
            "NSE,2,xyz,BAD2,BAD2-EQ,EQ,,0,",
            "NSE,2885,1,RELIANCE,RELIANCE-EQ,EQ,,0,",
        ]
    )
    s = _symbols(_Fetch(files={"NSE_Equity.csv": csv}))
    with caplog.at_level(logging.WARNING):
        assert await s.load("NSE") > 0
    assert caplog.text.count("flattrade_scrip_rows_skipped") == 1
    assert "skipped=2" in caplog.text
    assert (await s.resolve("RELIANCE")).token == "2885"
    with pytest.raises(KeyError):
        await s.resolve("BAD")
    assert len(await s.instruments("NSE")) == 1


def test_source_has_no_literal_bom_character() -> None:
    assert "﻿" not in Path(sym_mod.__file__).read_text(encoding="utf-8")
