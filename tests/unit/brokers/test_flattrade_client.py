"""Unit tests for src/brokers/adapters/flattrade/client.py and the Flattrade rate limits.

Payload shapes come from docs/superpowers/specs/2026-10-15-phase0-broker-api-findings.md
(A3, A7). Every HTTP call goes through an injected fake transport.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from src.brokers.adapters.flattrade.client import (
    ORDER_ENDPOINTS,
    FlattradeClient,
    FlattradeError,
    FlattradeSessionExpired,
    FlattradeTransportError,
    encode_body,
)
from src.brokers.rate_limiter import FLATTRADE_RATE_LIMITS, RateLimiter

_TOKEN = "SECRET-JKEY-0123456789abcdef"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, secs: float) -> None:
        self.now += secs


class FakeTransport:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def __call__(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
        self.calls.append((url, body, headers))
        return self.response


class RecordingLimiter:
    def __init__(self) -> None:
        self.categories: list[str] = []

    async def acquire(self, category: str = "general", tokens: float = 1.0) -> float:
        self.categories.append(category)
        return 0.0


def _client(response: Any, limiter: Any = None) -> tuple[FlattradeClient, FakeTransport]:
    transport = FakeTransport(response)
    client = FlattradeClient(
        transport=transport,
        limiter=limiter or RecordingLimiter(),
        user_id="FZ00000",
        access_token=_TOKEN,
    )
    return client, transport


# ── envelope handling ─────────────────────────────────────────────────────────


async def test_ok_dict_passes_through() -> None:
    resp = {"stat": "Ok", "request_time": "10:00:00", "norenordno": "24101500000001"}
    client, _ = _client(resp)
    assert await client.call("PlaceOrder", {"tsym": "RELIANCE-EQ"}) == resp


async def test_array_response_passes_through() -> None:
    resp = [{"norenordno": "1", "status": "OPEN"}, {"norenordno": "2", "status": "COMPLETE"}]
    client, _ = _client(resp)
    assert await client.call("OrderBook", {}) == resp


async def test_not_ok_raises_flattrade_error_with_emsg() -> None:
    client, _ = _client({"stat": "Not_Ok", "emsg": "Invalid Input : bad qty"})
    with pytest.raises(FlattradeError, match="Invalid Input : bad qty") as ei:
        await client.call("PlaceOrder", {})
    assert not isinstance(ei.value, FlattradeSessionExpired)


@pytest.mark.parametrize(
    "emsg",
    ["Session Expired : Invalid Session Key", "Invalid Session Key", "Session Expired :  "],
)
async def test_session_expired_raises_specific_error(emsg: str) -> None:
    client, _ = _client({"stat": "Not_Ok", "emsg": emsg})
    with pytest.raises(FlattradeSessionExpired):
        await client.call("UserDetails", {})


@pytest.mark.parametrize("stat", ["Oi created", "OI created", "OI replaced"])
async def test_gtt_style_success_stats_pass_through(stat: str) -> None:
    resp = {"request_time": "10:00:00", "stat": stat, "al_id": "21100800000009"}
    client, _ = _client(resp)
    assert await client.call("PlaceOCOOrder", {}) == resp


async def test_dict_without_stat_passes_through() -> None:
    resp = {"values": [{"tsym": "RELIANCE-EQ"}]}
    client, _ = _client(resp)
    assert await client.call("SearchScrip", {"stext": "RELIANCE"}) == resp


async def test_unknown_stat_fails_loud() -> None:
    client, _ = _client({"stat": "Something_New"})
    with pytest.raises(FlattradeError, match="Something_New"):
        await client.call("Limits", {})


async def test_non_json_shape_fails_loud() -> None:
    client, _ = _client("not a dict or list")
    with pytest.raises(FlattradeError):
        await client.call("Limits", {})


async def test_missing_token_raises_session_expired_without_network() -> None:
    transport = FakeTransport({"stat": "Ok"})
    client = FlattradeClient(transport=transport, limiter=RecordingLimiter(), user_id="FZ00000")
    with pytest.raises(FlattradeSessionExpired):
        await client.call("UserDetails", {})
    assert transport.calls == []


# ── request shape / encoding ──────────────────────────────────────────────────


async def test_request_body_has_jdata_jkey_and_uid_actid_defaults() -> None:
    client, transport = _client({"stat": "Ok"})
    await client.call("PlaceOrder", {"tsym": "M&M-EQ", "qty": "1"})
    url, body, _headers = transport.calls[0]
    assert url == "https://piconnect.flattrade.in/PiConnectAPI/PlaceOrder"
    assert body["jKey"] == _TOKEN
    jdata = json.loads(body["jData"])
    assert jdata == {"tsym": "M&M-EQ", "qty": "1", "uid": "FZ00000", "actid": "FZ00000"}


async def test_caller_uid_actid_are_not_overwritten() -> None:
    client, transport = _client({"stat": "Ok"})
    await client.call("Limits", {"uid": "OTHER", "actid": "OTHER2"})
    jdata = json.loads(transport.calls[0][1]["jData"])
    assert jdata["uid"] == "OTHER" and jdata["actid"] == "OTHER2"


async def test_caller_dict_is_not_mutated() -> None:
    client, _ = _client({"stat": "Ok"})
    data = {"tsym": "SBIN-EQ"}
    await client.call("PlaceOrder", data)
    assert data == {"tsym": "SBIN-EQ"}


def test_encode_body_is_the_raw_jdata_jkey_string() -> None:
    jdata = json.dumps({"tsym": "M&M-EQ", "uid": "FZ00000"}, separators=(",", ":"))
    encoded = encode_body({"jData": jdata, "jKey": "tok"})
    assert encoded == f"jData={jdata}&jKey=tok"
    assert "%" not in encoded  # Flattrade rejects a percent-encoded body


# ── bucket selection ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint",
    [
        "PlaceOrder",
        "ModifyOrder",
        "CancelOrder",
        "ExitSNOOrder",
        "PlaceGTTOrder",
        "ModifyGTTOrder",
        "CancelGTTOrder",
        "PlaceOCOOrder",
        "ModifyOCOOrder",
        "CancelOCOOrder",
    ],
)
async def test_order_endpoints_use_order_bucket(endpoint: str) -> None:
    limiter = RecordingLimiter()
    client, _ = _client({"stat": "Ok"}, limiter)
    await client.call(endpoint, {})
    assert limiter.categories == ["order"]
    assert endpoint in ORDER_ENDPOINTS


@pytest.mark.parametrize("endpoint", ["OrderBook", "UserDetails", "GetQuotes", "TPSeries"])
async def test_other_endpoints_use_general_bucket(endpoint: str) -> None:
    limiter = RecordingLimiter()
    client, _ = _client([], limiter)
    await client.call(endpoint, {})
    assert limiter.categories == ["general"]


async def test_order_endpoint_cannot_be_downgraded_to_general() -> None:
    limiter = RecordingLimiter()
    client, _ = _client({"stat": "Ok"}, limiter)
    await client.call("PlaceOrder", {}, bucket="general")
    assert limiter.categories == ["order"]


# ── secrets never logged ──────────────────────────────────────────────────────


async def test_token_never_in_log_records(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client, _ = _client({"stat": "Ok"})
    await client.call("PlaceOrder", {"tsym": "SBIN-EQ"})
    bad, _ = _client({"stat": "Not_Ok", "emsg": "Session Expired : Invalid Session Key"})
    with pytest.raises(FlattradeSessionExpired) as ei:
        await bad.call("UserDetails", {})
    assert caplog.records, "client should log calls (without secrets)"
    for rec in caplog.records:
        assert _TOKEN not in rec.getMessage()
        assert _TOKEN not in str(rec.__dict__)
    assert _TOKEN not in str(ei.value)
    assert _TOKEN not in repr(client)


# ── rate limits (fake clock) ──────────────────────────────────────────────────


def test_flattrade_rate_limit_numbers_match_findings() -> None:
    assert FLATTRADE_RATE_LIMITS["order"] == (10, 40)
    assert FLATTRADE_RATE_LIMITS["general"] == (40, 200)


async def test_41_order_acquisitions_take_at_least_60s() -> None:
    clock = FakeClock()
    limiter = RateLimiter.for_flattrade(clock=clock, sleep=clock.sleep)
    for _ in range(41):
        await limiter.acquire("order")
    assert clock.now >= 60.0


async def test_order_bucket_allows_burst_of_10_then_waits_for_next_second() -> None:
    clock = FakeClock()
    limiter = RateLimiter.for_flattrade(clock=clock, sleep=clock.sleep)
    for _ in range(10):
        assert await limiter.acquire("order") == 0.0
    assert clock.now == 0.0
    await limiter.acquire("order")  # 11th in the same second must wait
    assert clock.now >= 1.0


async def test_general_bucket_201_acquisitions_take_at_least_60s() -> None:
    clock = FakeClock()
    limiter = RateLimiter.for_flattrade(clock=clock, sleep=clock.sleep)
    for _ in range(201):
        await limiter.acquire("general")
    assert clock.now >= 60.0


async def test_no_one_second_window_exceeds_per_second_cap() -> None:
    clock = FakeClock()
    limiter = RateLimiter.for_flattrade(clock=clock, sleep=clock.sleep)
    stamps: list[float] = []
    for _ in range(120):
        await limiter.acquire("general")
        stamps.append(clock.now)
    for i, t in enumerate(stamps):
        in_window = [s for s in stamps[i:] if s < t + 1.0]
        assert len(in_window) <= 40
        in_minute = [s for s in stamps[i:] if s < t + 60.0]
        assert len(in_minute) <= 200


# ── session marker is case-insensitive (review M5) ────────────────────────────


@pytest.mark.parametrize("emsg", ["session expired : invalid session key", "INVALID SESSION KEY"])
async def test_session_marker_is_case_insensitive(emsg: str) -> None:
    client, _ = _client({"stat": "Not_Ok", "emsg": emsg})
    with pytest.raises(FlattradeSessionExpired):
        await client.call("UserDetails", {})


# ── default aiohttp transport: typed errors (review I2 / M2) ──────────────────


class _FakeResp:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    async def text(self) -> str:
        return self._text


class _FakePost:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    async def __aenter__(self) -> _FakeResp:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome  # type: ignore[no-any-return]

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    outcome: Any = None
    posted: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> _FakePost:
        _FakeSession.posted.append({"url": url, **kwargs})
        return _FakePost(_FakeSession.outcome)


@pytest.fixture
def fake_aiohttp(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSession]:
    import aiohttp

    _FakeSession.outcome = None
    _FakeSession.posted = []
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    return _FakeSession


def _real_transport_client() -> FlattradeClient:
    return FlattradeClient(limiter=RecordingLimiter(), user_id="FZ00000", access_token=_TOKEN)


async def test_default_transport_posts_form_body_and_parses_json(
    fake_aiohttp: type[_FakeSession],
) -> None:
    fake_aiohttp.outcome = _FakeResp(200, '{"stat":"Ok","actid":"FZ00000"}')
    out = await _real_transport_client().call("UserDetails", {})
    assert out["actid"] == "FZ00000"
    sent = fake_aiohttp.posted[0]
    assert sent["url"].endswith("/UserDetails")
    assert sent["data"].startswith("jData={") and sent["data"].endswith(f"&jKey={_TOKEN}")
    assert sent["headers"]["Content-Type"] == "application/json"


async def test_default_transport_timeout_is_ambiguous(fake_aiohttp: type[_FakeSession]) -> None:
    fake_aiohttp.outcome = TimeoutError()
    with pytest.raises(FlattradeTransportError) as ei:
        await _real_transport_client().call("PlaceOrder", {"tsym": "SBIN-EQ"})
    assert ei.value.ambiguous is True
    assert ei.value.endpoint == "PlaceOrder"
    assert str(ei.value)  # never an empty message
    assert _TOKEN not in str(ei.value) and "SBIN-EQ" not in str(ei.value)


async def test_default_transport_disconnect_after_send_is_ambiguous(
    fake_aiohttp: type[_FakeSession],
) -> None:
    import aiohttp

    fake_aiohttp.outcome = aiohttp.ServerDisconnectedError()
    with pytest.raises(FlattradeTransportError) as ei:
        await _real_transport_client().call("PlaceOrder", {})
    assert ei.value.ambiguous is True


async def test_default_transport_connect_failure_is_not_ambiguous(
    fake_aiohttp: type[_FakeSession],
) -> None:
    import aiohttp

    key = MagicMock()
    key.ssl = None
    fake_aiohttp.outcome = aiohttp.ClientConnectorError(key, OSError(11001, "getaddrinfo failed"))
    with pytest.raises(FlattradeTransportError) as ei:
        await _real_transport_client().call("PlaceOrder", {})
    assert ei.value.ambiguous is False
    assert _TOKEN not in str(ei.value)


async def test_default_transport_connect_timeout_is_not_ambiguous(
    fake_aiohttp: type[_FakeSession],
) -> None:
    import aiohttp

    fake_aiohttp.outcome = aiohttp.ConnectionTimeoutError()
    with pytest.raises(FlattradeTransportError) as ei:
        await _real_transport_client().call("PlaceOrder", {})
    assert ei.value.ambiguous is False


async def test_default_transport_non_json_reply_is_bounded(
    fake_aiohttp: type[_FakeSession],
) -> None:
    fake_aiohttp.outcome = _FakeResp(502, "<html>" + "x" * 5000 + "</html>")
    with pytest.raises(FlattradeError) as ei:
        await _real_transport_client().call("Limits", {})
    assert not isinstance(ei.value, FlattradeTransportError)
    assert "502" in str(ei.value)
    assert len(str(ei.value)) < 300


@pytest.mark.parametrize(
    "resp",
    [
        _FakeResp(502, "<html>Bad Gateway</html>"),  # non-JSON gateway page
        _FakeResp(200, "not json"),  # non-JSON with a 200
        _FakeResp(503, '{"stat":"Not_Ok","emsg":"busy"}'),  # 5xx even with JSON
    ],
)
async def test_order_endpoint_non_json_or_5xx_is_ambiguous_transport(
    fake_aiohttp: type[_FakeSession], resp: _FakeResp
) -> None:
    """A gateway error says nothing about whether the OMS took the order (review I2)."""
    fake_aiohttp.outcome = resp
    with pytest.raises(FlattradeTransportError) as ei:
        await _real_transport_client().call("PlaceOrder", {"tsym": "SBIN-EQ"})
    assert ei.value.ambiguous is True
    assert ei.value.endpoint == "PlaceOrder"
    assert _TOKEN not in str(ei.value)


async def test_non_order_endpoint_5xx_json_is_unwrapped_as_before(
    fake_aiohttp: type[_FakeSession],
) -> None:
    fake_aiohttp.outcome = _FakeResp(503, '{"stat":"Not_Ok","emsg":"busy"}')
    with pytest.raises(FlattradeError) as ei:
        await _real_transport_client().call("Limits", {})
    assert not isinstance(ei.value, FlattradeTransportError)


@pytest.mark.parametrize("endpoint", ["PlaceOrder", "ModifyOrder", "CancelOrder"])
async def test_server_timeout_on_order_endpoint_is_ambiguous(endpoint: str) -> None:
    client, _ = _client({"stat": "Not_Ok", "emsg": "Server Timeout :  "})
    with pytest.raises(FlattradeTransportError) as ei:
        await client.call(endpoint, {})
    assert ei.value.ambiguous is True


async def test_server_timeout_on_non_order_endpoint_stays_plain_error() -> None:
    client, _ = _client({"stat": "Not_Ok", "emsg": "Server Timeout :  "})
    with pytest.raises(FlattradeError) as ei:
        await client.call("OrderBook", {})
    assert not isinstance(ei.value, FlattradeTransportError)


# ── RateLimiter(buckets=...) constructor argument (review M3) ─────────────────


def test_rate_limiter_rejects_buckets_for_unknown_category() -> None:
    from src.brokers.rate_limiter import SlidingWindowBucket

    with pytest.raises(ValueError, match="unknown categories"):
        RateLimiter({"general": 1.0}, buckets={"order": SlidingWindowBucket([(1, 1.0)])})
