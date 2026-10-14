"""Regression 2026-09-08: NSE's own index CSVs carry temporary "DUMMY*"
when-issued placeholder symbols during a demerger/spin-off reconstitution
(e.g. DUMMYVEDL1-4 for the Vedanta demerger). These have no instrument token
or historical data on Kite and failed every OHLCV fetch, every night,
forever — _fetch_index_symbols must filter them out at parse time."""

from __future__ import annotations

from unittest.mock import patch

from src.market_data.ohlcv_downloader import _fetch_index_symbols

_FAKE_CSV = (
    b"Company Name,Industry,Symbol,Series,ISIN Code\r\n"
    b"Infosys Ltd,IT,INFY,EQ,INE009A01021\r\n"
    b"Dummy Vedanta 1,Metals,DUMMYVEDL1,EQ,INE000A00001\r\n"
    b"Dummy Vedanta 2,Metals,DUMMYVEDL2,EQ,INE000A00002\r\n"
    b"Reliance Industries,Energy,RELIANCE,EQ,INE002A01018\r\n"
)


class _FakeResponse:
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def read(self) -> bytes:
        return _FAKE_CSV


def test_fetch_index_symbols_drops_dummy_placeholders(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cache_path = tmp_path / "nifty500_symbols.json"
    with patch("urllib.request.urlopen", return_value=_FakeResponse()):
        symbols = _fetch_index_symbols("nifty500", "http://fake", cache_path, min_expected=2)

    assert symbols == ["INFY", "RELIANCE"]
    assert "DUMMYVEDL1" not in symbols
    assert "DUMMYVEDL2" not in symbols
