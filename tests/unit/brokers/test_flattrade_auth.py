"""Unit tests for src/brokers/adapters/flattrade/auth.py and src/brokers/flattrade_auth.py.

Login is manual-assisted by design (Flattrade terms 2(a)/2(e)): nothing here
automates auth.flattrade.in. All HTTP goes through injected fakes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from src.brokers import flattrade_auth
from src.brokers.adapters.flattrade.auth import FlattradeAuth
from src.brokers.adapters.flattrade.client import (
    FlattradeClient,
    FlattradeError,
    FlattradeSessionExpired,
)

_TOKEN = "SECRET-JKEY-feedface"
_SECRET = "API-SECRET-cafebabe"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI load_dotenv()s into os.environ; setenv first so monkeypatch restores it.
    for k in ("FLATTRADE_API_KEY", "FLATTRADE_USER_ID", "FLATTRADE_ACCESS_TOKEN"):
        monkeypatch.setenv(k, "")


class FakeTransport:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def __call__(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
        self.calls.append((url, body, headers))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class NoLimit:
    async def acquire(self, category: str = "general", tokens: float = 1.0) -> float:
        return 0.0


def _auth(
    *, api_response: Any = None, exchange_response: Any = None, token: str = ""
) -> tuple[FlattradeAuth, FakeTransport, FakeTransport]:
    api = FakeTransport(api_response)
    exch = FakeTransport(exchange_response)
    client = FlattradeClient(transport=api, limiter=NoLimit(), user_id="FZ00000")
    auth = FlattradeAuth(api_key="APIKEY1", user_id="FZ00000", client=client, transport=exch)
    if token:
        auth.set_access_token(token)
    return auth, api, exch


# ── construction ──────────────────────────────────────────────────────────────


def test_from_env_reads_key_user_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLATTRADE_API_KEY", "k1")
    monkeypatch.setenv("FLATTRADE_USER_ID", "FZ11111")
    monkeypatch.setenv("FLATTRADE_ACCESS_TOKEN", _TOKEN)
    auth = FlattradeAuth.from_env()
    assert auth.api_key == "k1"
    assert auth.user_id == "FZ11111"
    assert auth.access_token == _TOKEN
    assert auth.client.has_token


def test_from_env_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLATTRADE_API_KEY", raising=False)
    monkeypatch.setenv("FLATTRADE_USER_ID", "FZ11111")
    with pytest.raises(KeyError):
        FlattradeAuth.from_env()


def test_repr_hides_token() -> None:
    auth, _, _ = _auth(token=_TOKEN)
    assert _TOKEN not in repr(auth)


# ── login / refresh are manual-assisted ───────────────────────────────────────


async def test_login_raises_with_cli_instruction() -> None:
    auth, api, exch = _auth()
    with pytest.raises(RuntimeError, match=r"python -m src\.brokers\.flattrade_auth"):
        await auth.login()
    assert api.calls == [] and exch.calls == []


async def test_refresh_token_raises_with_cli_instruction() -> None:
    auth, api, exch = _auth(token=_TOKEN)
    with pytest.raises(RuntimeError, match=r"python -m src\.brokers\.flattrade_auth"):
        await auth.refresh_token()
    assert api.calls == [] and exch.calls == []


def test_login_url() -> None:
    auth, _, _ = _auth()
    assert auth.login_url() == "https://auth.flattrade.in/?app_key=APIKEY1"


# ── get_session_status ────────────────────────────────────────────────────────


async def test_session_status_valid_via_user_details() -> None:
    resp = {"stat": "Ok", "actid": "FZ00000", "exarr": ["NSE"], "orarr": ["LMT"]}
    auth, api, _ = _auth(api_response=resp, token=_TOKEN)
    status = await auth.get_session_status()
    assert status.is_valid and status.user_id == "FZ00000"
    url, body, _ = api.calls[0]
    assert url.endswith("/UserDetails")
    assert json.loads(body["jData"])["uid"] == "FZ00000"


async def test_session_status_expired_is_invalid() -> None:
    resp = {"stat": "Not_Ok", "emsg": "Session Expired : Invalid Session Key"}
    auth, _, _ = _auth(api_response=resp, token=_TOKEN)
    status = await auth.get_session_status()
    assert not status.is_valid
    assert "Session Expired" in status.message


async def test_session_status_without_token_is_invalid_and_offline() -> None:
    auth, api, _ = _auth(api_response={"stat": "Ok"})
    status = await auth.get_session_status()
    assert not status.is_valid
    assert api.calls == []


async def test_session_status_other_errors_propagate() -> None:
    auth, _, _ = _auth(api_response={"stat": "Not_Ok", "emsg": "Server Timeout :  "}, token=_TOKEN)
    with pytest.raises(FlattradeError):
        await auth.get_session_status()
    auth2, _, _ = _auth(api_response=OSError("network down"), token=_TOKEN)
    with pytest.raises(OSError):
        await auth2.get_session_status()


# ── request-code exchange ─────────────────────────────────────────────────────


def test_hash_vector() -> None:
    expected = hashlib.sha256(b"APIKEY1" + b"REQCODE9" + _SECRET.encode()).hexdigest()
    assert FlattradeAuth.secret_hash("APIKEY1", "REQCODE9", _SECRET) == expected
    assert len(expected) == 64


@pytest.mark.parametrize("stat_key", ["stat", "status"])
async def test_exchange_success_both_stat_spellings(stat_key: str) -> None:
    resp = {"token": _TOKEN, "client": "FZ00000", stat_key: "Ok", "emsg": ""}
    auth, _, exch = _auth(exchange_response=resp)
    token = await auth.exchange_request_code("REQCODE9", _SECRET)
    assert token == _TOKEN
    assert auth.access_token == _TOKEN and auth.client.has_token
    url, body, _ = exch.calls[0]
    assert url == "https://authapi.flattrade.in/trade/apitoken"
    assert body == {
        "api_key": "APIKEY1",
        "request_code": "REQCODE9",
        "api_secret": hashlib.sha256(f"APIKEY1REQCODE9{_SECRET}".encode()).hexdigest(),
    }
    assert _SECRET not in json.dumps(body)


@pytest.mark.parametrize(
    "resp",
    [
        {"stat": "Not_Ok", "emsg": "Invalid request code"},
        {"status": "Ok", "emsg": ""},  # no token
        {"token": _TOKEN, "stat": "Not_Ok", "emsg": "x"},
        {"token": _TOKEN, "stat": "Ok", "client": "FZ99999"},  # someone else's account
        ["not", "a", "dict"],
    ],
)
async def test_exchange_failure_raises(resp: Any) -> None:
    auth, _, _ = _auth(exchange_response=resp)
    with pytest.raises(FlattradeError) as ei:
        await auth.exchange_request_code("REQCODE9", _SECRET)
    assert _TOKEN not in str(ei.value) and _SECRET not in str(ei.value)
    assert not auth.client.has_token


async def test_exchange_error_shows_reply_fields_without_the_token() -> None:
    resp = {
        "stat": "Not_Ok",
        "message": "Invalid api secret",
        "token": _TOKEN,
        "susertoken": "SESSIONSECRET123",
    }
    auth, _, _ = _auth(exchange_response=resp)
    with pytest.raises(FlattradeError) as ei:
        await auth.exchange_request_code("REQCODE9", _SECRET)
    assert "Invalid api secret" in str(ei.value)
    assert "susertoken" in str(ei.value) and "SESSIONSECRET123" not in str(ei.value)
    assert _TOKEN not in str(ei.value) and _SECRET not in str(ei.value)


async def test_exchange_never_logs_secrets(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    resp = {"token": _TOKEN, "client": "FZ00000", "stat": "Ok"}
    auth, _, _ = _auth(exchange_response=resp)
    await auth.exchange_request_code("REQCODE9", _SECRET)
    for rec in caplog.records:
        text = rec.getMessage() + str(rec.__dict__)
        assert _TOKEN not in text and _SECRET not in text


# ── CLI: pasted code parsing ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pasted", "expected"),
    [
        ("https://127.0.0.1:5000/cb?code=abc123", "abc123"),
        ("https://example.com/redirect?request_code=xyz789&client=FZ00000", "xyz789"),
        ("code=abc123", "abc123"),
        ("request_code=xyz789", "xyz789"),
        ("  plaincode42  ", "plaincode42"),
    ],
)
def test_parse_request_code(pasted: str, expected: str) -> None:
    assert flattrade_auth.parse_request_code(pasted) == expected


@pytest.mark.parametrize("pasted", ["", "   ", "https://example.com/redirect?client=FZ1", "a b"])
def test_parse_request_code_rejects_garbage(pasted: str) -> None:
    with pytest.raises(ValueError):
        flattrade_auth.parse_request_code(pasted)


# ── CLI: manual-assisted login writes the token ───────────────────────────────


def _env_file(tmp_path: Path, extra: str = "") -> Path:
    env = tmp_path / ".env"
    env.write_text(f"FLATTRADE_API_KEY=APIKEY1\nFLATTRADE_USER_ID=FZ00000\n{extra}", "utf-8")
    return env


def _patch_auth_factory(
    monkeypatch: pytest.MonkeyPatch, api_response: Any = None, exchange_response: Any = None
) -> list[FlattradeAuth]:
    built: list[FlattradeAuth] = []

    def factory(api_key: str, user_id: str) -> FlattradeAuth:
        client = FlattradeClient(
            transport=FakeTransport(api_response), limiter=NoLimit(), user_id=user_id
        )
        auth = FlattradeAuth(
            api_key=api_key,
            user_id=user_id,
            client=client,
            transport=FakeTransport(exchange_response),
        )
        built.append(auth)
        return auth

    monkeypatch.setattr(flattrade_auth, "FlattradeAuth", factory)
    return built


def test_run_login_exchanges_pasted_url_and_writes_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _env_file(tmp_path)
    _patch_auth_factory(
        monkeypatch, exchange_response={"token": _TOKEN, "client": "FZ00000", "stat": "Ok"}
    )
    monkeypatch.setattr(flattrade_auth, "_get_api_secret", lambda user_id: _SECRET)
    monkeypatch.setattr("builtins.input", lambda prompt="": "https://x.test/cb?code=abc123")

    token = flattrade_auth.run_login(env)

    assert token == _TOKEN
    assert f"FLATTRADE_ACCESS_TOKEN='{_TOKEN}'" in env.read_text("utf-8") or (
        f"FLATTRADE_ACCESS_TOKEN={_TOKEN}" in env.read_text("utf-8")
    )
    out = capsys.readouterr().out
    assert "https://auth.flattrade.in/?app_key=APIKEY1" in out
    assert "5 AM IST" in out
    assert _TOKEN not in out


def test_run_login_without_secret_points_to_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _env_file(tmp_path)
    _patch_auth_factory(monkeypatch)
    monkeypatch.setattr(flattrade_auth, "_get_api_secret", lambda user_id: None)
    with pytest.raises(RuntimeError, match="--setup"):
        flattrade_auth.run_login(env)


def test_run_setup_stores_secret_in_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        flattrade_auth.keyring, "set_password", lambda s, u, p: stored.__setitem__((s, u), p)
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": "FZ00000")
    monkeypatch.setattr(flattrade_auth.getpass, "getpass", lambda prompt="": _SECRET)
    flattrade_auth.run_setup()
    assert stored == {("flattrade_api_secret", "FZ00000"): _SECRET}


# ── auto_refresh never logs in ────────────────────────────────────────────────


def test_auto_refresh_returns_valid_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = _env_file(tmp_path, f"FLATTRADE_ACCESS_TOKEN={_TOKEN}\n")
    built = _patch_auth_factory(monkeypatch, api_response={"stat": "Ok", "actid": "FZ00000"})
    assert flattrade_auth.auto_refresh(env) == _TOKEN
    assert built[0].access_token == _TOKEN


@pytest.mark.parametrize(
    ("env_extra", "force"),
    [
        (f"FLATTRADE_ACCESS_TOKEN={_TOKEN}\n", False),  # expired per UserDetails
        ("", False),  # no token at all
        (f"FLATTRADE_ACCESS_TOKEN={_TOKEN}\n", True),  # forced: still cannot log in
    ],
)
def test_auto_refresh_never_logs_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_extra: str, force: bool
) -> None:
    env = _env_file(tmp_path, env_extra)
    built = _patch_auth_factory(
        monkeypatch,
        api_response={"stat": "Not_Ok", "emsg": "Session Expired : Invalid Session Key"},
        exchange_response=AssertionError("token exchange must never run"),
    )
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": pytest.fail("auto_refresh must not prompt")
    )
    with pytest.raises(RuntimeError, match=r"run python -m src\.brokers\.flattrade_auth"):
        flattrade_auth.auto_refresh(env, force=force)
    for auth in built:
        assert auth._transport.calls == []  # type: ignore[attr-defined]  # noqa: SLF001


def test_auto_refresh_propagates_non_session_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _env_file(tmp_path, f"FLATTRADE_ACCESS_TOKEN={_TOKEN}\n")
    _patch_auth_factory(monkeypatch, api_response={"stat": "Not_Ok", "emsg": "Server Timeout :  "})
    with pytest.raises(FlattradeError) as ei:
        flattrade_auth.auto_refresh(env)
    assert not isinstance(ei.value, FlattradeSessionExpired)


# ── review fixes: paste cleanup (M1), actid check (M6), typed exchange errors (I2) ─


@pytest.mark.parametrize(
    "pasted",
    [
        '"https://x.test/cb?code=abc123"',
        "'https://x.test/cb?code=abc123'",
        "<https://x.test/cb?code=abc123>",
        "  https://x.test/cb?code=abc123.  ",
        "https://x.test/cb?code=abc123,",
        '"abc123"',
        "<abc123>;",
    ],
)
def test_parse_request_code_strips_wrapping_and_trailing_punctuation(pasted: str) -> None:
    assert flattrade_auth.parse_request_code(pasted) == "abc123"


async def test_session_status_actid_mismatch_is_invalid() -> None:
    auth, _, _ = _auth(api_response={"stat": "Ok", "actid": "FZ99999"}, token=_TOKEN)
    status = await auth.get_session_status()
    assert not status.is_valid
    assert "FZ99999" in status.message and "FZ00000" in status.message


async def test_session_status_actid_case_insensitive_match_is_valid() -> None:
    auth, _, _ = _auth(api_response={"stat": "Ok", "actid": "fz00000"}, token=_TOKEN)
    assert (await auth.get_session_status()).is_valid


class _ExchFakeSession:
    outcome: Any = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _ExchFakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> Any:
        outcome = _ExchFakeSession.outcome

        class _Ctx:
            async def __aenter__(self) -> Any:
                raise outcome

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Ctx()


@pytest.mark.parametrize(("exc_name", "ambiguous"), [("timeout", True), ("connect", False)])
async def test_exchange_transport_errors_are_typed(
    monkeypatch: pytest.MonkeyPatch, exc_name: str, ambiguous: bool
) -> None:
    from unittest.mock import MagicMock

    import aiohttp
    from src.brokers.adapters.flattrade.client import FlattradeTransportError

    key = MagicMock()
    key.ssl = None
    _ExchFakeSession.outcome = (
        TimeoutError()
        if exc_name == "timeout"
        else aiohttp.ClientConnectorError(key, OSError(11001, "getaddrinfo failed"))
    )
    monkeypatch.setattr(aiohttp, "ClientSession", _ExchFakeSession)
    auth = FlattradeAuth(api_key="APIKEY1", user_id="FZ00000")  # default transports
    with pytest.raises(FlattradeTransportError) as ei:
        await auth.exchange_request_code("REQCODE9", _SECRET)
    assert ei.value.ambiguous is ambiguous
    assert _SECRET not in str(ei.value) and "REQCODE9" not in str(ei.value)
