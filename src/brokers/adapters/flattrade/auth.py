"""
Flattrade (Pi API v2) session manager — accepts a stored daily token.

Login is MANUAL-ASSISTED by design (findings A2, C-1..C-3; Flattrade terms
2(a)/2(e)): a human logs in at https://auth.flattrade.in/?app_key=<API_KEY> in
a browser, the redirect carries a one-time `code` / `request_code`, and
`python -m src.brokers.flattrade_auth` exchanges it for the session token.
Nothing in this package scripts the browser login, and login()/refresh_token()
never try to: there is no documented refresh grant, and tokens are cleared
daily between 5 and 6 AM IST.

Token exchange (findings A2 step 4/5):
  POST https://authapi.flattrade.in/trade/apitoken
  JSON {"api_key", "request_code", "api_secret": sha256(api_key + request_code + api_secret)}
  -> {"token", "client", "status"|"stat": "Ok", "emsg"}
The exchange is only honoured from the API key's registered static IP.

Credentials: FLATTRADE_API_KEY / FLATTRADE_USER_ID in .env, the API secret in
the OS keyring (see flattrade_auth.py), the token in FLATTRADE_ACCESS_TOKEN.
The token and secret are never logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any
from urllib.parse import quote

from src.brokers.adapters.flattrade.client import (
    FlattradeClient,
    FlattradeError,
    FlattradeSessionExpired,
    Transport,
    aiohttp_post,
)
from src.brokers.base import SessionInfo, SessionStatus

_log = logging.getLogger(__name__)

LOGIN_URL = "https://auth.flattrade.in/"
TOKEN_URL = "https://authapi.flattrade.in/trade/apitoken"  # noqa: S105 — a URL, not a secret
MANUAL_LOGIN_HINT = "run python -m src.brokers.flattrade_auth"


async def _aiohttp_json_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
    """Default transport for the token exchange: POST JSON, parse the JSON reply.
    Failures are typed (FlattradeTransportError) by the shared aiohttp_post."""
    return await aiohttp_post(url, json.dumps(body), headers)


_SAFE_REPLY_FIELDS = ("stat", "status", "message", "msg", "error", "reason", "detail", "code")


def _reply_summary(resp: dict[str, Any]) -> str:
    """Describe a token reply that has no `emsg`. Only status-style fields are echoed;
    every other field is listed by NAME only, so no credential can reach an error or log."""
    shown = {
        k: str(resp[k])[:120] for k in _SAFE_REPLY_FIELDS if isinstance(resp.get(k), str | int)
    }
    other = sorted(k for k in resp if k not in shown)
    return f"no token in reply (reply fields: {shown}; other field names: {other})"


class FlattradeAuth:
    """Holds the API key, client id and the day's session token."""

    def __init__(
        self,
        api_key: str,
        user_id: str,
        *,
        client: FlattradeClient | None = None,
        transport: Transport | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._client = client if client is not None else FlattradeClient(user_id=user_id)
        self._transport: Transport = transport or _aiohttp_json_post
        self._access_token = ""

    def __repr__(self) -> str:  # never expose the token
        return f"FlattradeAuth(user_id={self._user_id!r}, has_token={bool(self._access_token)})"

    @classmethod
    def from_env(cls) -> FlattradeAuth:
        """Construct from FLATTRADE_API_KEY / FLATTRADE_USER_ID (KeyError if absent),
        plus FLATTRADE_ACCESS_TOKEN when present."""
        instance = cls(
            api_key=os.environ["FLATTRADE_API_KEY"],
            user_id=os.environ["FLATTRADE_USER_ID"],
        )
        token = os.environ.get("FLATTRADE_ACCESS_TOKEN", "")
        if token:
            instance.set_access_token(token)
        return instance

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def client(self) -> FlattradeClient:
        """The API client the adapter uses; shares this session's token."""
        return self._client

    def set_access_token(self, token: str) -> None:
        self._access_token = token
        self._client.set_access_token(token)

    async def login(self) -> SessionInfo:
        """Never logs in on its own: the daily login needs a human in a browser."""
        raise RuntimeError(f"Flattrade login is manual-assisted; {MANUAL_LOGIN_HINT}")

    async def refresh_token(self) -> SessionInfo:
        """No refresh grant exists (findings A2); same manual step as login()."""
        raise RuntimeError(f"Flattrade has no token refresh; {MANUAL_LOGIN_HINT}")

    async def get_session_status(self) -> SessionStatus:
        """Validate the stored token with UserDetails.

        Returns is_valid=False when there is no token or the API reports the
        session expired; any other error (network, server) propagates so a
        transient outage is not mistaken for a dead token.
        """
        if not self._client.has_token:
            return SessionStatus(is_valid=False, user_id="", message="no access token")
        try:
            data = await self._client.call("UserDetails", {})
        except FlattradeSessionExpired as exc:
            return SessionStatus(is_valid=False, user_id="", message=str(exc))
        actid = str((data.get("actid") if isinstance(data, dict) else None) or "")
        if actid and self._user_id and actid.upper() != self._user_id.upper():
            # Same guard as exchange_request_code: a token for another account is not ours.
            return SessionStatus(
                is_valid=False,
                user_id=actid,
                message=f"token belongs to {actid!r}, expected {self._user_id!r}",
            )
        return SessionStatus(is_valid=True, user_id=actid or self._user_id)

    def login_url(self) -> str:
        """The browser login page for this API key."""
        return f"{LOGIN_URL}?app_key={quote(self._api_key, safe='')}"

    @staticmethod
    def secret_hash(api_key: str, request_code: str, api_secret: str) -> str:
        """SHA-256 hex of the plain concatenation api_key + request_code + api_secret."""
        return hashlib.sha256(f"{api_key}{request_code}{api_secret}".encode()).hexdigest()

    async def exchange_request_code(self, request_code: str, api_secret: str) -> str:
        """Exchange a one-time request code for the session token and keep it.

        Accepts `stat` or `status` == "Ok" (the doc sample and the OpenAlgo
        code disagree, findings A2). A `client` field naming a different
        account than FLATTRADE_USER_ID is rejected.

        Raises:
            FlattradeError: the exchange was refused or the reply is unusable.
        """
        body = {
            "api_key": self._api_key,
            "request_code": request_code,
            "api_secret": self.secret_hash(self._api_key, request_code, api_secret),
        }
        resp = await self._transport(TOKEN_URL, body, {"Content-Type": "application/json"})
        if not isinstance(resp, dict):
            raise FlattradeError(f"token exchange: unexpected reply type {type(resp).__name__}")
        ok = resp.get("stat") == "Ok" or resp.get("status") == "Ok"
        token = resp.get("token")
        if not ok or not isinstance(token, str) or not token:
            emsg = resp.get("emsg") or _reply_summary(resp)
            raise FlattradeError(f"token exchange failed: {emsg}")
        client = str(resp.get("client") or "")
        if client and self._user_id and client.upper() != self._user_id.upper():
            raise FlattradeError(
                f"token exchange returned client {client!r}, expected {self._user_id!r}"
            )
        self.set_access_token(token)
        _log.info("flattrade_token_exchanged user_id=%s", self._user_id)
        return token
