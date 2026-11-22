"""Unit tests for src/brokers/adapters/kite/auth.py."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from src.brokers.adapters.kite.auth import KiteAuth

_IST = ZoneInfo("Asia/Kolkata")


def _make_auth(**kwargs: str) -> KiteAuth:
    return KiteAuth(
        api_key=kwargs.get("api_key", "test_api_key"),
        api_secret=kwargs.get("api_secret", "test_secret"),
        user_id=kwargs.get("user_id", "AB1234"),
    )


# ── from_env ───────────────────────────────────────────────────────────────────


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KITE_API_KEY", "key123")
    monkeypatch.setenv("KITE_SECRET", "sec456")
    monkeypatch.setenv("KITE_USER_ID", "XY9999")
    auth = KiteAuth.from_env()
    assert auth._api_key == "key123"
    assert auth._api_secret == "sec456"
    assert auth._user_id == "XY9999"


def test_from_env_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    with pytest.raises(KeyError):
        KiteAuth.from_env()


# ── set_access_token / kite property ──────────────────────────────────────────


def test_set_access_token() -> None:
    auth = _make_auth()
    auth.set_access_token("my_token")
    assert auth.kite.access_token == "my_token"


# ── login ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_login_happy_path() -> None:
    auth = _make_auth()

    fake_session_data = {
        "access_token": "tok_abc123",
        "user_id": "AB1234",
        "user_name": "Test User",
    }

    with (
        patch("src.brokers.adapters.kite.auth.keyring.get_password") as mock_kr,
        patch("src.brokers.adapters.kite.auth._do_web_login", new_callable=AsyncMock) as mock_web,
        patch.object(auth._kite, "generate_session", return_value=fake_session_data),
    ):
        mock_kr.side_effect = ["password123", "JBSWY3DPEHPK3PXP"]
        mock_web.return_value = "req_token_xyz"

        info = await auth.login()

    assert info.access_token == "tok_abc123"
    assert info.user_id == "AB1234"
    assert isinstance(info.login_time, datetime)
    mock_web.assert_awaited_once()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_login_missing_password_raises() -> None:
    auth = _make_auth()

    with (
        patch("src.brokers.adapters.kite.auth.keyring.get_password", return_value=None),
        pytest.raises(ValueError, match="No login password"),
    ):
        await auth.login()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_login_missing_totp_raises() -> None:
    auth = _make_auth()

    with (
        patch(
            "src.brokers.adapters.kite.auth.keyring.get_password",
            side_effect=["password123", None],
        ),
        pytest.raises(ValueError, match="No TOTP secret"),
    ):
        await auth.login()


# ── refresh_token ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_refresh_token_delegates_to_login() -> None:
    auth = _make_auth()

    fake_info = MagicMock()
    with patch.object(auth, "login", new_callable=AsyncMock, return_value=fake_info) as mock_login:
        result = await auth.refresh_token()

    assert result is fake_info
    mock_login.assert_awaited_once()


# ── get_session_status ─────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_session_status_valid() -> None:
    auth = _make_auth()

    with patch.object(
        auth._kite, "profile", return_value={"user_id": "AB1234", "user_name": "Test"}
    ):
        status = await auth.get_session_status()

    assert status.is_valid is True
    assert status.user_id == "AB1234"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_session_status_invalid_on_exception() -> None:
    auth = _make_auth()

    with patch.object(auth._kite, "profile", side_effect=Exception("Token expired")):
        status = await auth.get_session_status()

    assert status.is_valid is False
    assert "Token expired" in status.message
