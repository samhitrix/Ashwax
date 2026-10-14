"""TelegramTransport tests via an injected fake aiohttp-shaped session — no
real network calls."""

from __future__ import annotations

from src.notifications.config import TransportConfig
from src.notifications.telegram import TelegramTransport, _redact


class _FakeResponse:
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.closed = False

    def post(self, url: str, *, json: dict, timeout: object) -> _FakeResponse:
        self.calls.append({"url": url, "json": json})
        if not self._responses:
            raise AssertionError("no more fake responses queued")
        return self._responses.pop(0)

    async def close(self) -> None:
        self.closed = True


def _cfg(**overrides: object) -> TransportConfig:
    base = {
        "api_base": "https://api.telegram.org",
        "timeout_seconds": 8.0,
        "max_retries": 2,
        "backoff_seconds": 0.001,  # keep tests fast
        "parse_mode": "HTML",
        "alert_after_consecutive_failures": 5,
    }
    base.update(overrides)
    return TransportConfig(**base)  # type: ignore[arg-type]


async def test_send_success_first_try() -> None:
    session = _FakeSession([_FakeResponse(200)])
    t = TelegramTransport("TOKEN", "123", _cfg(), session_factory=lambda: session)
    ok, err = await t.send("hello")
    assert ok is True
    assert err == ""
    assert len(session.calls) == 1


async def test_retries_on_429_then_succeeds() -> None:
    session = _FakeSession([_FakeResponse(429, "rate limited"), _FakeResponse(200)])
    t = TelegramTransport("TOKEN", "123", _cfg(), session_factory=lambda: session)
    ok, err = await t.send("hello")
    assert ok is True
    assert len(session.calls) == 2


async def test_retries_on_5xx_then_succeeds() -> None:
    session = _FakeSession([_FakeResponse(500, "server error"), _FakeResponse(200)])
    t = TelegramTransport("TOKEN", "123", _cfg(), session_factory=lambda: session)
    ok, err = await t.send("hello")
    assert ok is True


async def test_gives_up_after_max_retries_returns_false_not_raise() -> None:
    session = _FakeSession(
        [_FakeResponse(500, "e1"), _FakeResponse(500, "e2"), _FakeResponse(500, "e3")]
    )
    t = TelegramTransport("TOKEN", "123", _cfg(max_retries=2), session_factory=lambda: session)
    ok, err = await t.send("hello")
    assert ok is False
    assert err != ""
    assert len(session.calls) == 3  # initial + 2 retries


async def test_non_retryable_4xx_fails_immediately() -> None:
    session = _FakeSession([_FakeResponse(400, "bad request")])
    t = TelegramTransport("TOKEN", "123", _cfg(), session_factory=lambda: session)
    ok, err = await t.send("hello")
    assert ok is False
    assert len(session.calls) == 1  # no retry on 4xx (non-429)


async def test_missing_credentials_returns_false_without_network() -> None:
    session = _FakeSession([])
    t = TelegramTransport("", "", _cfg(), session_factory=lambda: session)
    ok, err = await t.send("hello")
    assert ok is False
    assert session.calls == []


async def test_exception_from_session_is_caught_and_retried() -> None:
    class _RaisingSession(_FakeSession):
        def post(self, url: str, *, json: dict, timeout: object) -> _FakeResponse:
            self.calls.append({"url": url, "json": json})
            raise ConnectionError("network unreachable")

    session = _RaisingSession([])
    t = TelegramTransport("TOKEN", "123", _cfg(max_retries=1), session_factory=lambda: session)
    ok, err = await t.send("hello")
    assert ok is False
    assert len(session.calls) == 2  # initial + 1 retry


def test_redact_strips_token_from_error_string() -> None:
    text = "http_403: forbidden for bot123SECRETTOKEN in https://api.telegram.org/bot123SECRETTOKEN/sendMessage"
    redacted = _redact(text, "123SECRETTOKEN")
    assert "123SECRETTOKEN" not in redacted
    assert "***" in redacted


async def test_token_never_appears_in_returned_error_string() -> None:
    token = "SUPERSECRET123"
    session = _FakeSession([_FakeResponse(400, f"bad request for bot{token}")])
    t = TelegramTransport(token, "123", _cfg(), session_factory=lambda: session)
    ok, err = await t.send("hello")
    assert ok is False
    assert token not in err


async def test_aclose_closes_session() -> None:
    session = _FakeSession([_FakeResponse(200)])
    t = TelegramTransport("TOKEN", "123", _cfg(), session_factory=lambda: session)
    await t.send("hello")
    await t.aclose()
    assert session.closed is True
