"""
Angel One (SmartAPI) authentication manager.

Handles TOTP login and JWT session refresh. All credentials come from env
vars / OS keyring — never hardcoded.

SmartAPI token flow (verified against angel-one/smartapi-python's
generateSession/generateToken source, smartapi.angelbroking.com/docs/User):
  1. SmartConnect.generateSession(client_code, pin, totp) -> jwtToken +
     refreshToken + feedToken. `pin` is the 4-digit trading PIN, NOT the
     web-login password.
  2. SmartConnect.generateToken(refresh_token) -> a fresh jwtToken + feedToken
     WITHOUT needing TOTP again. Unlike Kite (no refresh grant — only full
     daily re-login), Angel One's refresh token is the primary renewal path;
     full login() is the fallback only if the refresh token itself has
     expired or been invalidated.

The underlying SmartConnect SDK (`SmartApi` package) is fully synchronous
(built on `requests`) — every call here is offloaded via asyncio.to_thread so
an Angel One auth cycle can never block the event loop.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import keyring
import pyotp
from SmartApi import SmartConnect

from src.brokers.base import SessionInfo, SessionStatus

_IST = ZoneInfo("Asia/Kolkata")


class AngelOneAuth:
    """
    Angel One authentication manager.

    Credentials are read from:
      - ANGEL_ONE_API_KEY, ANGEL_ONE_CLIENT_CODE  (environment variables)
      - keyring("angel_one_login", client_code)    -> 4-digit trading PIN
      - keyring("angel_one_totp",  client_code)    -> TOTP secret (base32)
    """

    def __init__(self, api_key: str, client_code: str) -> None:
        self._api_key = api_key
        self._client_code = client_code
        self._sc = SmartConnect(api_key=api_key)
        self._refresh_token: str = ""

    @classmethod
    def from_env(cls) -> AngelOneAuth:
        """Construct from ANGEL_ONE_* environment variables, including access
        token if present."""
        instance = cls(
            api_key=os.environ["ANGEL_ONE_API_KEY"],
            client_code=os.environ["ANGEL_ONE_CLIENT_CODE"],
        )
        token = os.environ.get("ANGEL_ONE_ACCESS_TOKEN", "")
        if token:
            instance.set_access_token(token)
        refresh = os.environ.get("ANGEL_ONE_REFRESH_TOKEN", "")
        if refresh:
            instance._refresh_token = refresh
        return instance

    def set_access_token(self, token: str) -> None:
        """Configure the SmartConnect instance with a known access (JWT) token."""
        self._sc.setAccessToken(token)

    @property
    def sc(self) -> SmartConnect:
        """Expose the configured SmartConnect instance for the adapter."""
        return self._sc

    async def login(self) -> SessionInfo:
        """
        Full TOTP login.

        Reads credentials from keyring:
          keyring.set_password("angel_one_login", client_code, pin)
          keyring.set_password("angel_one_totp",  client_code, totp_secret)
        """
        pin = keyring.get_password("angel_one_login", self._client_code)
        if not pin:
            raise ValueError(
                f"No trading PIN in keyring for '{self._client_code}'. Store it with: "
                'python -c "import keyring; keyring.set_password('
                "'angel_one_login', 'CLIENT_CODE', 'PIN')\""
            )

        totp_secret = keyring.get_password("angel_one_totp", self._client_code)
        if not totp_secret:
            raise ValueError(
                f"No TOTP secret in keyring for '{self._client_code}'. Store it with: "
                'python -c "import keyring; keyring.set_password('
                "'angel_one_totp', 'CLIENT_CODE', 'BASE32_SECRET')\""
            )

        totp = pyotp.TOTP(totp_secret).now()
        data: dict[str, Any] = await asyncio.to_thread(
            self._sc.generateSession, self._client_code, pin, totp
        )
        if not data.get("status"):
            raise RuntimeError(f"Angel One login failed: {data.get('message', 'no message')}")

        payload: dict[str, Any] = data["data"]
        self._refresh_token = str(payload["refreshToken"])
        # generateSession() already calls self._sc.setAccessToken(jwtToken) and
        # setFeedToken(feedToken) internally — self._sc.access_token is current.
        access_token = str(self._sc.access_token)

        return SessionInfo(
            access_token=access_token,
            user_id=str(payload.get("clientcode", self._client_code)),
            login_time=datetime.now(_IST),
        )

    async def refresh_token(self) -> SessionInfo:
        """Renew the JWT via the refresh token; falls back to a full TOTP
        login only if the refresh token itself is no longer valid."""
        if not self._refresh_token:
            return await self.login()

        try:
            data: dict[str, Any] = await asyncio.to_thread(
                self._sc.generateToken, self._refresh_token
            )
            if not data.get("status"):
                raise RuntimeError(str(data.get("message", "refresh rejected")))
            payload: dict[str, Any] = data["data"]
            # generateToken() already calls setAccessToken/setFeedToken internally.
            return SessionInfo(
                access_token=str(payload.get("jwtToken", self._sc.access_token)),
                user_id=self._client_code,
                login_time=datetime.now(_IST),
            )
        except Exception:  # noqa: BLE001
            return await self.login()

    async def get_session_status(self) -> SessionStatus:
        """
        Validate the current access token via SmartAPI's profile endpoint.
        Returns SessionStatus(is_valid=False) on any error.
        """
        try:
            profile: dict[str, Any] = await asyncio.to_thread(
                self._sc.getProfile, self._refresh_token
            )
            if not profile.get("status"):
                return SessionStatus(
                    is_valid=False, user_id="", message=str(profile.get("message", ""))
                )
            data: dict[str, Any] = profile.get("data") or {}
            return SessionStatus(is_valid=True, user_id=str(data.get("clientcode", "")))
        except Exception as exc:  # noqa: BLE001
            return SessionStatus(is_valid=False, user_id="", message=str(exc))
