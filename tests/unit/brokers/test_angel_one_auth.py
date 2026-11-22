"""Unit tests for src/brokers/adapters/angel_one/auth.py."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from src.brokers.adapters.angel_one.auth import AngelOneAuth

_IST = ZoneInfo("Asia/Kolkata")


def _make_auth(**kwargs: str) -> AngelOneAuth:
    return AngelOneAuth(
        api_key=kwargs.get("api_key", "test_api_key"),
        client_code=kwargs.get("client_code", "C123456"),
    )


# ── from_env ───────────────────────────────────────────────────────────────────


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANGEL_ONE_API_KEY", "key123")
    monkeypatch.setenv("ANGEL_ONE_CLIENT_CODE", "C999999")
    monkeypatch.delenv("ANGEL_ONE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ANGEL_ONE_REFRESH_TOKEN", raising=False)
    auth = AngelOneAuth.from_env()
    assert auth._api_key == "key123"
    assert auth._client_code == "C999999"


def test_from_env_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANGEL_ONE_API_KEY", raising=False)
    with pytest.raises(KeyError):
        AngelOneAuth.from_env()


def test_from_env_picks_up_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANGEL_ONE_API_KEY", "key123")
    monkeypatch.setenv("ANGEL_ONE_CLIENT_CODE", "C999999")
    monkeypatch.delenv("ANGEL_ONE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("ANGEL_ONE_REFRESH_TOKEN", "rt_abc")
    auth = AngelOneAuth.from_env()
    assert auth._refresh_token == "rt_abc"


# ── set_access_token / sc property ────────────────────────────────────────────


def test_set_access_token() -> None:
    auth = _make_auth()
    auth.set_access_token("my_token")
    assert auth.sc.access_token == "my_token"


# ── login ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_login_happy_path() -> None:
    auth = _make_auth()

    fake_response = {
        "status": True,
        "message": "SUCCESS",
        "data": {
            "clientcode": "C123456",
            "jwtToken": "jwt_abc",
            "refreshToken": "refresh_abc",
            "feedToken": "feed_abc",
        },
    }

    def _fake_generate_session(client_code: str, pin: str, totp: str) -> dict:  # type: ignore[type-arg]
        auth.sc.setAccessToken(fake_response["data"]["jwtToken"])
        return fake_response

    with (
        patch("src.brokers.adapters.angel_one.auth.keyring.get_password") as mock_kr,
        patch.object(auth._sc, "generateSession", side_effect=_fake_generate_session),
    ):
        mock_kr.side_effect = ["1234", "JBSWY3DPEHPK3PXP"]

        info = await auth.login()

    assert info.access_token == "jwt_abc"
    assert info.user_id == "C123456"
    assert isinstance(info.login_time, datetime)
    assert auth._refresh_token == "refresh_abc"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_login_missing_pin_raises() -> None:
    auth = _make_auth()

    with (
        patch("src.brokers.adapters.angel_one.auth.keyring.get_password", return_value=None),
        pytest.raises(ValueError, match="No trading PIN"),
    ):
        await auth.login()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_login_missing_totp_raises() -> None:
    auth = _make_auth()

    with (
        patch(
            "src.brokers.adapters.angel_one.auth.keyring.get_password",
            side_effect=["1234", None],
        ),
        pytest.raises(ValueError, match="No TOTP secret"),
    ):
        await auth.login()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_login_failure_raises_runtime_error() -> None:
    auth = _make_auth()

    with (
        patch("src.brokers.adapters.angel_one.auth.keyring.get_password") as mock_kr,
        patch.object(
            auth._sc,
            "generateSession",
            return_value={"status": False, "message": "Invalid credentials"},
        ),
    ):
        mock_kr.side_effect = ["1234", "JBSWY3DPEHPK3PXP"]
        with pytest.raises(RuntimeError, match="Invalid credentials"):
            await auth.login()


# ── refresh_token ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_refresh_token_no_refresh_token_falls_back_to_login() -> None:
    auth = _make_auth()
    fake_info = MagicMock()

    with patch.object(auth, "login", new_callable=AsyncMock, return_value=fake_info) as mock_login:
        result = await auth.refresh_token()

    assert result is fake_info
    mock_login.assert_awaited_once()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_refresh_token_uses_generate_token() -> None:
    auth = _make_auth()
    auth._refresh_token = "refresh_abc"

    fake_response = {
        "status": True,
        "data": {"jwtToken": "new_jwt", "refreshToken": "refresh_abc", "feedToken": "new_feed"},
    }
    with patch.object(auth._sc, "generateToken", return_value=fake_response):
        info = await auth.refresh_token()

    assert info.access_token == "new_jwt"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_refresh_token_falls_back_to_login_on_rejection() -> None:
    auth = _make_auth()
    auth._refresh_token = "expired_refresh"
    fake_info = MagicMock()

    with (
        patch.object(
            auth._sc, "generateToken", return_value={"status": False, "message": "expired"}
        ),
        patch.object(auth, "login", new_callable=AsyncMock, return_value=fake_info) as mock_login,
    ):
        result = await auth.refresh_token()

    assert result is fake_info
    mock_login.assert_awaited_once()


# ── get_session_status ─────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_session_status_valid() -> None:
    auth = _make_auth()

    with patch.object(
        auth._sc,
        "getProfile",
        return_value={"status": True, "data": {"clientcode": "C123456"}},
    ):
        status = await auth.get_session_status()

    assert status.is_valid is True
    assert status.user_id == "C123456"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_session_status_invalid_status_false() -> None:
    auth = _make_auth()

    with patch.object(
        auth._sc, "getProfile", return_value={"status": False, "message": "Token expired"}
    ):
        status = await auth.get_session_status()

    assert status.is_valid is False
    assert "Token expired" in status.message


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_session_status_invalid_on_exception() -> None:
    auth = _make_auth()

    with patch.object(auth._sc, "getProfile", side_effect=Exception("Network error")):
        status = await auth.get_session_status()

    assert status.is_valid is False
    assert "Network error" in status.message
