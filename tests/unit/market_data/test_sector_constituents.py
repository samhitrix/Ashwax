"""Unit tests for NSE sector-index constituent fetching/caching."""

from __future__ import annotations

import json

import src.market_data.sector_constituents as sc


def test_cache_hit_returns_cached_dict_without_http_call(tmp_path, monkeypatch):
    cache = tmp_path / "sector_constituents.json"
    cache.write_text(json.dumps({"NIFTY BANK": ["HDFCBANK", "ICICIBANK"]}))
    monkeypatch.setattr(sc, "_CACHE_PATH", cache)

    def _boom(*a, **kw):
        raise AssertionError("should not fetch when cache is present")

    monkeypatch.setattr(sc, "_fetch_one_sector_csv", _boom)

    result = sc.fetch_sector_constituents()
    assert result == {"NIFTY BANK": ["HDFCBANK", "ICICIBANK"]}


def test_one_sector_fetch_failure_omits_only_that_sector(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_CACHE_PATH", tmp_path / "sector_constituents.json")

    def _fake_fetch(sector, url):
        return None if sector == "NIFTY BANK" else [f"{sector}_SYM"]

    monkeypatch.setattr(sc, "_fetch_one_sector_csv", _fake_fetch)

    result = sc.fetch_sector_constituents(force_refresh=True)
    assert "NIFTY BANK" not in result
    assert len(result) == len(sc._SECTOR_CSV_URLS) - 1


def test_symbol_to_sector_map_reverse_lookup():
    constituents = {"NIFTY AUTO": ["MARUTI", "TATAMOTORS"], "NIFTY BANK": ["HDFCBANK"]}
    mapping = sc.symbol_to_sector_map(constituents)
    assert mapping["MARUTI"] == "NIFTY AUTO"
    assert mapping["HDFCBANK"] == "NIFTY BANK"
    assert len(mapping) == 3
