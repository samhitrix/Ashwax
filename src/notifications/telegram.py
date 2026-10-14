"""Telegram Bot API transport. Never raises — every failure mode returns
(False, redacted_error) so the worker can log/persist/retry without a
try/except at every call site.

Retry/backoff idiom mirrors src/llm/providers.py:_dispatch_with_retry
(delay = 1.0; delay *= 2.0 on each retry).

aiohttp is used (not httpx) because it is already a de-facto runtime dependency
of this codebase (src/api/server.py, src/llm/providers.py, src/orchestrator.py
all import it directly today, though undeclared in pyproject.toml).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Protocol, cast

from src.notifications.config import TransportConfig

_LOG = logging.getLogger(__name__)  # "src.notifications.telegram" -> notifications.log

_REDACTED = "***"


def _redact(text: str, bot_token: str) -> str:
    """Strip the bot token out of any error string before it can reach a log
    line, a DB row, or launcher.py's stdout passthrough to the console."""
    if not bot_token:
        return text
    return text.replace(bot_token, _REDACTED)


class _Response(Protocol):
    status: int

    async def text(self) -> str: ...
    async def __aenter__(self) -> _Response: ...
    async def __aexit__(self, *exc: object) -> None: ...


class _ClientSession(Protocol):
    # aiohttp.ClientSession.post() is NOT an async def — it's a plain method
    # returning an object that is both awaitable and an async context manager
    # (_RequestContextManager). Modelling it as `async def` here would type
    # its return as a bare coroutine, which `async with` cannot be used on.
    def post(self, url: str, *, json: dict[str, Any], timeout: Any) -> _Response: ...  # noqa: ASYNC109
    async def close(self) -> None: ...


class TelegramTransport:
    """Sends one message per `send()` call. The aiohttp session is created
    lazily on first use, inside the worker's own event loop — never at import
    time or in __init__ — because a session bound to a different loop is a
    known freeze source in this codebase's history (see eventloop_block
    incident)."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        cfg: TransportConfig,
        session_factory: Callable[[], _ClientSession] | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._cfg = cfg
        self._session_factory = session_factory
        self._session: _ClientSession | None = None

    def _url(self) -> str:
        return f"{self._cfg.api_base}/bot{self._bot_token}/sendMessage"

    async def _get_session(self) -> _ClientSession:
        # Built and returned via a local, not self._session directly — mypy
        # narrows locals across statements but not instance attributes, and
        # both branches below always leave `session` non-None.
        session = self._session
        if session is None:
            if self._session_factory is not None:
                session = self._session_factory()
            else:
                import aiohttp

                # aiohttp.ClientSession structurally over-satisfies _ClientSession
                # (it's the real thing the Protocol models) — cast rather than
                # fight mypy's structural check on a library type we don't own.
                session = cast("_ClientSession", aiohttp.ClientSession())
            self._session = session
        return session

    async def send(self, text: str) -> tuple[bool, str]:
        """Returns (ok, error). Never raises."""
        if not self._bot_token or not self._chat_id:
            return False, "missing bot_token/chat_id"

        delay = self._cfg.backoff_seconds
        last_error = ""
        for attempt in range(self._cfg.max_retries + 1):
            try:
                session = await self._get_session()
                import aiohttp

                timeout = aiohttp.ClientTimeout(total=self._cfg.timeout_seconds)
                async with session.post(
                    self._url(),
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "parse_mode": self._cfg.parse_mode,
                        "disable_web_page_preview": True,
                    },
                    timeout=timeout,
                ) as resp:
                    if resp.status == 200:
                        return True, ""
                    body = await resp.text()
                    last_error = _redact(f"http_{resp.status}: {body[:200]}", self._bot_token)
                    # rate-limited or transient server error -> retry
                    if (
                        resp.status == 429 or resp.status >= 500
                    ) and attempt < self._cfg.max_retries:
                        await asyncio.sleep(delay)
                        delay *= 2.0
                        continue
                    return False, last_error
            except TimeoutError:
                last_error = "timeout"
            except Exception as exc:
                last_error = _redact(str(exc), self._bot_token)

            if attempt < self._cfg.max_retries:
                await asyncio.sleep(delay)
                delay *= 2.0

        return False, last_error or "unknown_error"

    async def aclose(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception as exc:
                _LOG.warning(
                    "telegram_session_close_failed err=%s", _redact(str(exc), self._bot_token)
                )
            self._session = None
