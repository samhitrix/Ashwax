"""A machine without the Angel One SDK (`SmartApi`) must still boot when Angel is not in use.

2026-10-17 regression: boot imported angel_one_auth unconditionally, the orchestrator died with
ModuleNotFoundError and the launcher took the whole stack (dashboard included) down.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from src.market_data.provider_config import CREDENTIAL_ENV, refresh_provider_credentials


@pytest.fixture
def no_smartapi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "SmartApi", None)  # `import SmartApi` -> ImportError
    for mod in ("src.brokers.angel_one_auth", "src.brokers.adapters.angel_one.auth"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    for names in CREDENTIAL_ENV.values():
        for key in names:
            monkeypatch.delenv(key, raising=False)


def test_refresh_without_angel_credentials_never_imports_the_sdk(no_smartapi: None) -> None:
    assert refresh_provider_credentials(["kite", "angel_one", "flattrade"], Path(".env")) == {}


def test_refresh_with_angel_credentials_but_no_sdk_is_a_logged_failure(
    no_smartapi: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in CREDENTIAL_ENV["angel_one"]:
        monkeypatch.setenv(key, "x")
    assert refresh_provider_credentials(["angel_one"], Path(".env")) == {"angel_one": False}


def test_angel_refresh_loop_without_angel_brokers_returns_without_the_sdk(
    no_smartapi: None,
) -> None:
    from src.orchestrator import _angel_token_refresh_loop

    asyncio.run(_angel_token_refresh_loop({}, asyncio.Event()))
