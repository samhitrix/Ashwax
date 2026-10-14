"""
Kite authentication manager.

Handles TOTP web login and session validation.
All credentials come from env vars / machine-wide DPAPI storage — never hardcoded.

Kite token flow (daily):
  1. POST /api/login (user_id + password) → request_id
  2. POST /api/twofa (request_id + TOTP) → redirect with ?request_token=
  3. kite.generate_session(request_token) → access_token

Kite access tokens expire at midnight IST — there is no refresh; only re-login.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import aiohttp
import pyotp
from kiteconnect import KiteConnect

from src.brokers.base import SessionInfo, SessionStatus
from src.brokers.machine_secrets import get_machine_secret

_IST = ZoneInfo("Asia/Kolkata")

_LOGIN_URL = "https://kite.zerodha.com/api/login"
_TWOFA_URL = "https://kite.zerodha.com/api/twofa"
_KITE_VERSION_HEADER = {"X-Kite-Version": "3"}


async def _do_web_login(user_id: str, password: str, totp: str) -> str:
    """
    Perform Kite web login and return the request_token.

    Steps:
      1. POST /api/login → request_id in JSON body
      2. POST /api/twofa → redirect to redirect_url?request_token=...
    """
    async with aiohttp.ClientSession(headers=_KITE_VERSION_HEADER) as http:
        # Step 1: password login
        async with http.post(
            _LOGIN_URL,
            data={"user_id": user_id, "password": password},
        ) as r1:
            r1.raise_for_status()
            body1: dict[str, Any] = await r1.json()

        if body1.get("status") != "success":
            raise RuntimeError(f"Kite login failed: {body1.get('message', 'no message')}")
        request_id = str(body1["data"]["request_id"])

        # Step 2: TOTP 2FA — follow redirect to extract request_token from URL
        async with http.post(
            _TWOFA_URL,
            data={
                "user_id": user_id,
                "request_id": request_id,
                "twofa_value": totp,
                "twofa_type": "totp",
            },
            allow_redirects=True,
        ) as r2:
            r2.raise_for_status()
            final_url = str(r2.url)

    params = parse_qs(urlparse(final_url).query)
    token_list = params.get("request_token", [])
    if not token_list:
        raise RuntimeError(
            f"request_token not found in redirect URL: {final_url!r}. "
            "Check that the redirect URL is configured in the Kite app console."
        )
    return token_list[0]


class KiteAuth:
    """
    Kite authentication manager.

    Credentials are read from:
      - KITE_API_KEY, KITE_SECRET, KITE_USER_ID           (environment variables)
      - machine_secrets "kite_login:{user_id}"            → login password
      - machine_secrets "kite_totp:{user_id}"             → TOTP secret (base32)
    """

    def __init__(self, api_key: str, api_secret: str, user_id: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._user_id = user_id
        self._kite = KiteConnect(api_key=api_key)

    @classmethod
    def from_env(cls) -> KiteAuth:
        """Construct from KITE_* environment variables, including access token if present."""
        instance = cls(
            api_key=os.environ["KITE_API_KEY"],
            api_secret=os.environ["KITE_SECRET"],
            user_id=os.environ["KITE_USER_ID"],
        )
        token = os.environ.get("KITE_ACCESS_TOKEN", "")
        if token:
            instance.set_access_token(token)
        return instance

    def set_access_token(self, token: str) -> None:
        """Configure the KiteConnect instance with a known access token."""
        self._kite.set_access_token(token)

    @property
    def kite(self) -> KiteConnect:
        """Expose the configured KiteConnect instance for the adapter."""
        return self._kite

    async def login(self) -> SessionInfo:
        """
        Full TOTP web login.

        Reads credentials from machine_secrets:
          set_machine_secret(f"kite_login:{user_id}", password)
          set_machine_secret(f"kite_totp:{user_id}", totp_secret)
        """
        password = get_machine_secret(f"kite_login:{self._user_id}")
        if not password:
            raise ValueError(
                f"No login password stored for '{self._user_id}'. Store it with: "
                'python -c "from src.brokers.machine_secrets import set_machine_secret; '
                f"set_machine_secret('kite_login:{self._user_id}', 'PASSWORD')\""
            )

        totp_secret = get_machine_secret(f"kite_totp:{self._user_id}")
        if not totp_secret:
            raise ValueError(
                f"No TOTP secret stored for '{self._user_id}'. Store it with: "
                'python -c "from src.brokers.machine_secrets import set_machine_secret; '
                f"set_machine_secret('kite_totp:{self._user_id}', 'BASE32_SECRET')\""
            )

        totp = pyotp.TOTP(totp_secret).now()
        request_token = await _do_web_login(self._user_id, password, totp)

        data: dict[str, Any] = self._kite.generate_session(
            request_token, api_secret=self._api_secret
        )
        access_token = str(data["access_token"])
        self._kite.set_access_token(access_token)

        return SessionInfo(
            access_token=access_token,
            user_id=str(data.get("user_id", self._user_id)),
            login_time=datetime.now(_IST),
        )

    async def refresh_token(self) -> SessionInfo:
        """Kite tokens expire at midnight IST — re-login is the only path."""
        return await self.login()

    async def get_session_status(self) -> SessionStatus:
        """
        Validate the current access token via Kite profile API.
        Returns SessionStatus(is_valid=False) on any error.
        """
        try:
            profile: dict[str, Any] = self._kite.profile()
            return SessionStatus(is_valid=True, user_id=str(profile.get("user_id", "")))
        except Exception as exc:  # noqa: BLE001
            return SessionStatus(is_valid=False, user_id="", message=str(exc))
