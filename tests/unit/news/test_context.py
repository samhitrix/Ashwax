"""Unit tests for src/news/context.py — one-line news summary for G9."""

from __future__ import annotations

from src.news.context import build_news_context


def _shared_with(symbol: str) -> dict:
    return {
        "news_recent": {
            symbol: {
                "sentiment": "BULLISH",
                "action": "BOOST",
                "magnitude": "HIGH",
                "headline": "Acme beats estimates",
                "reasoning": "record profit",
                "ts": 1000.0,
            }
        }
    }


def test_returns_formatted_string_when_present() -> None:
    out = build_news_context("ACME", _shared_with("ACME"))
    assert out == "BULLISH/HIGH (BOOST): Acme beats estimates — record profit"


def test_returns_none_when_symbol_absent() -> None:
    assert build_news_context("OTHER", _shared_with("ACME")) is None


def test_returns_none_when_cache_empty() -> None:
    assert build_news_context("ACME", {}) is None
