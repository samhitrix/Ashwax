"""
Flattrade Pi API v2 (Noren OMS) HTTP client.

Wire facts (docs/superpowers/specs/2026-10-15-phase0-broker-api-findings.md, A3/A7):
  - Base URL https://piconnect.flattrade.in/PiConnectAPI/ ; every call is a POST
    to <base><Endpoint> with the RAW body `jData=<json>&jKey=<session token>` (not URL-encoded).
  - Success: a JSON object with `stat: "Ok"` (GTT/OCO create/modify return
    "Oi created" / "OI created" / "OI replaced"), a JSON array (list endpoints),
    or an object without `stat` (some endpoints).
  - Failure: `{"stat": "Not_Ok", "emsg": "..."}`; an expired/invalid token says
    "Session Expired : Invalid Session Key".
  - Numeric request fields are strings; converting them is the caller's job.

The session token (`jKey`) is a bearer credential: it is never logged and never
included in an exception message.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

_log = logging.getLogger(__name__)  # nests under "src.brokers" -> brokers.log

DEFAULT_BASE_URL = "https://piconnect.flattrade.in/PiConnectAPI/"

# Async transport: (url, body, headers) -> parsed JSON. Injected in tests.
Transport = Callable[[str, dict[str, Any], dict[str, str]], Awaitable[Any]]

# Endpoints paced by the order bucket (10/s, 40/min); everything else is "general".
ORDER_ENDPOINTS: frozenset[str] = frozenset(
    {
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
    }
)

_OK_STATS: frozenset[str] = frozenset({"Ok", "Oi created", "OI created", "OI replaced"})
_SESSION_MARKERS = ("Session Expired", "Invalid Session Key")
# A Not_Ok whose emsg starts with this (findings A7: `"Server Timeout :  "`) says the
# OMS did not answer in time - on an order endpoint the order may still exist.
_SERVER_TIMEOUT_MARKER = "server timeout"


class FlattradeError(RuntimeError):
    """The Flattrade API answered with an error (or an unusable response)."""


class FlattradeSessionExpired(FlattradeError):
    """The session token is missing, expired or invalid — a manual re-login is needed."""


class FlattradeTransportError(FlattradeError):
    """The HTTP exchange itself failed (no usable reply).

    `ambiguous` is True when the request may have reached Flattrade and its
    outcome is unknown (read timeout, disconnect after sending) — for an order
    endpoint the caller must reconcile against the order book before retrying.
    The message names only the endpoint and the exception type, never the body,
    token or secret.
    """

    def __init__(self, endpoint: str, *, ambiguous: bool, cause: BaseException) -> None:
        self.endpoint = endpoint
        self.ambiguous = ambiguous
        outcome = "outcome unknown, request may have reached Flattrade" if ambiguous else "not sent"
        super().__init__(f"{endpoint}: transport failure {type(cause).__name__} ({outcome})")


class _Limiter(Protocol):
    async def acquire(self, category: str = "general", tokens: float = 1.0) -> float: ...


def encode_body(body: dict[str, str]) -> str:
    """The wire body: the RAW string ``jData=<json>&jKey=<token>``.

    Flattrade rejects a URL-encoded body ("jData is not valid json object", verified live
    2026-10-17), so nothing is percent-encoded here.
    """
    return f"jData={body['jData']}&jKey={body['jKey']}"


async def aiohttp_post(url: str, data: str, headers: dict[str, str]) -> Any:
    """POST `data` with aiohttp and parse the JSON reply; failures are typed.

    Raises:
        FlattradeTransportError: the HTTP exchange failed. `ambiguous` is False
            only when the connection was never established (DNS, refused,
            connect timeout), i.e. the request certainly did not reach Flattrade.
        FlattradeError: the reply is not JSON (message carries a bounded snippet).
            On an ORDER endpoint (ORDER_ENDPOINTS) a non-JSON reply or any HTTP 5xx
            is a FlattradeTransportError(ambiguous=True) instead: a gateway error
            page says nothing about whether the OMS took the order, so the caller
            must read the order book before any retry.
    """
    import aiohttp

    endpoint = url.rstrip("/").rsplit("/", 1)[-1]
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(url, data=data, headers=headers) as resp,
        ):
            status = resp.status
            text = await resp.text()
    # ConnectionTimeoutError subclasses the (ambiguous) timeout types, so it is caught first.
    except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError) as exc:
        raise FlattradeTransportError(endpoint, ambiguous=False, cause=exc) from exc
    except (TimeoutError, aiohttp.ClientError) as exc:
        raise FlattradeTransportError(endpoint, ambiguous=True, cause=exc) from exc
    is_order = endpoint in ORDER_ENDPOINTS
    if is_order and status >= 500:
        raise FlattradeTransportError(
            endpoint, ambiguous=True, cause=FlattradeError(f"HTTP {status}")
        )
    try:
        return json.loads(text)
    except ValueError as exc:
        if is_order:
            raise FlattradeTransportError(endpoint, ambiguous=True, cause=exc) from exc
        raise FlattradeError(
            f"{endpoint}: HTTP {status}: non-JSON response ({text[:120]!r})"
        ) from exc


async def _aiohttp_form_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
    """Default transport: POST the raw jData/jKey body and parse the JSON reply.

    Content-Type is application/json as in the vendor curl sample; Flattrade also accepted
    text/plain and form-urlencoded for the same raw body (verified live on UserDetails).
    """
    return await aiohttp_post(url, encode_body(body), headers)


class FlattradeClient:
    """Thin async client: builds the jData/jKey body, paces it, unwraps the envelope."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
        limiter: _Limiter | None = None,
        *,
        user_id: str = "",
        access_token: str = "",
    ) -> None:
        if limiter is None:
            from src.brokers.rate_limiter import RateLimiter

            limiter = RateLimiter.for_flattrade()
        self._base_url = base_url if base_url.endswith("/") else base_url + "/"
        self._transport: Transport = transport or _aiohttp_form_post
        self._limiter = limiter
        self._user_id = user_id
        self._token = access_token

    def __repr__(self) -> str:  # never expose the token
        return f"FlattradeClient(base_url={self._base_url!r}, user_id={self._user_id!r})"

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def set_access_token(self, token: str) -> None:
        self._token = token

    async def call(self, endpoint: str, data: dict[str, Any], *, bucket: str = "general") -> Any:
        """POST `data` to `endpoint` and return the unwrapped JSON.

        `uid`/`actid` default to the client id unless the caller sets them.
        Order endpoints always use the "order" bucket regardless of `bucket`.

        Sending `actid` to endpoints whose doc table lists only `uid` assumes the
        OMS ignores the extra field — UNVERIFIED (no live call made).

        Raises:
            FlattradeSessionExpired: no token, or the API reports the session expired.
            FlattradeError: `stat == "Not_Ok"`, an unknown `stat`, or a non-JSON shape.
            FlattradeTransportError: (ambiguous) a "Server Timeout" Not_Ok on an order
                endpoint - the order may exist; read the order book before retrying.
        """
        if not self._token:
            raise FlattradeSessionExpired(
                "No Flattrade session token; run python -m src.brokers.flattrade_auth"
            )
        category = "order" if endpoint in ORDER_ENDPOINTS else bucket
        payload = {"uid": self._user_id, "actid": self._user_id, **data}
        body = {"jData": json.dumps(payload, separators=(",", ":")), "jKey": self._token}
        headers = {"Content-Type": "application/json"}

        await self._limiter.acquire(category)
        started = time.monotonic()
        result = await self._transport(self._base_url + endpoint, body, headers)
        elapsed_ms = (time.monotonic() - started) * 1000
        try:
            out = self._unwrap(endpoint, result)
        except FlattradeError as exc:
            _log.warning(
                "flattrade_call_failed endpoint=%s bucket=%s ms=%.0f error=%s",
                endpoint,
                category,
                elapsed_ms,
                exc,
            )
            raise
        _log.debug("flattrade_call endpoint=%s bucket=%s ms=%.0f", endpoint, category, elapsed_ms)
        return out

    @staticmethod
    def _unwrap(endpoint: str, result: Any) -> Any:
        if isinstance(result, list):
            return result
        if not isinstance(result, dict):
            raise FlattradeError(f"{endpoint}: unexpected response type {type(result).__name__}")
        if "stat" not in result:
            return result
        stat = result["stat"]
        if stat == "Not_Ok":
            emsg = str(result.get("emsg") or "no emsg")
            if any(m.lower() in emsg.lower() for m in _SESSION_MARKERS):
                raise FlattradeSessionExpired(f"{endpoint}: {emsg}")
            if endpoint in ORDER_ENDPOINTS and emsg.strip().lower().startswith(
                _SERVER_TIMEOUT_MARKER
            ):
                raise FlattradeTransportError(
                    endpoint, ambiguous=True, cause=FlattradeError(f"{endpoint}: {emsg}")
                )
            raise FlattradeError(f"{endpoint}: {emsg}")
        if stat in _OK_STATS:
            return result
        # Fail loud on a status we have never seen documented rather than treat it as success.
        raise FlattradeError(f"{endpoint}: unexpected stat {stat!r}")
