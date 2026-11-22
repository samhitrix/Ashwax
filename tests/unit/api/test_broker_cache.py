"""Tests for the API's short-lived broker read cache (src/api/server.py).

The dashboard polls ~8 broker-backed endpoints on one timer. Each was an
independent, uncoalesced Zerodha round-trip, and _kite() rebuilt its KiteConnect
(and therefore its requests.Session) per call, so every read also paid a fresh TLS
handshake. These cover the caching/coalescing behaviour that replaced that.
"""

from __future__ import annotations

import threading

from src.api import server


def test_second_call_inside_ttl_is_served_from_cache() -> None:
    calls = []

    def fetch() -> str:
        calls.append(1)
        return "payload"

    assert server._broker_cached("k", 60.0, fetch) == "payload"
    assert server._broker_cached("k", 60.0, fetch) == "payload"
    assert len(calls) == 1


def test_expired_ttl_refetches() -> None:
    calls = []

    def fetch() -> int:
        calls.append(1)
        return len(calls)

    assert server._broker_cached("k", 0.0, fetch) == 1
    assert server._broker_cached("k", 0.0, fetch) == 2
    assert len(calls) == 2


def test_distinct_keys_do_not_share_a_slot() -> None:
    assert server._broker_cached("a", 60.0, lambda: "A") == "A"
    assert server._broker_cached("b", 60.0, lambda: "B") == "B"
    assert server._broker_cached("a", 60.0, lambda: "changed") == "A"


def test_failure_is_not_cached() -> None:
    """A broker error must not be latched for the TTL — the next poll retries.

    This is what lets /api/session keep surfacing a genuinely dead token promptly
    despite caching the profile payload.
    """
    state = {"fail": True}

    def fetch() -> str:
        if state["fail"]:
            raise RuntimeError("kite down")
        return "recovered"

    try:
        server._broker_cached("k", 60.0, fetch)
    except RuntimeError:
        pass
    else:  # pragma: no cover - guards against the exception being swallowed
        raise AssertionError("expected the fetch error to propagate")

    state["fail"] = False
    assert server._broker_cached("k", 60.0, fetch) == "recovered"


def test_concurrent_callers_share_one_fetch() -> None:
    """Single-flight: N threads asking for the same cold key make ONE broker call."""
    calls: list[int] = []
    started = threading.Event()

    def fetch() -> str:
        calls.append(1)
        started.set()
        # Hold the fetch open so the other threads are provably queued behind it.
        threading.Event().wait(0.2)
        return "payload"

    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(server._broker_cached("k", 60.0, fetch)))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert results == ["payload"] * 8
    assert len(calls) == 1


def test_clear_broker_cache_drops_cached_values() -> None:
    assert server._broker_cached("k", 60.0, lambda: "first") == "first"
    server._clear_broker_cache()
    assert server._broker_cached("k", 60.0, lambda: "second") == "second"
