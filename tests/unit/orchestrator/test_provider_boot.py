"""Boot-time provider credential refresh (no Kite needed on a Kite-less machine)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.market_data.provider_config import refresh_provider_credentials


def test_only_providers_with_credentials_are_refreshed(monkeypatch) -> None:
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.setenv("ANGEL_ONE_API_KEY", "a")
    monkeypatch.setenv("ANGEL_ONE_CLIENT_CODE", "c")
    kite, angel = MagicMock(return_value="t"), MagicMock(return_value="t")
    out = refresh_provider_credentials(
        ["kite", "angel_one"], Path(".env"), {"kite": kite, "angel_one": angel}
    )
    kite.assert_not_called()
    angel.assert_called_once()
    assert out == {"angel_one": True}


def test_refresh_failure_is_contained(monkeypatch) -> None:
    monkeypatch.setenv("KITE_API_KEY", "k")
    kite = MagicMock(side_effect=RuntimeError("login failed"))
    out = refresh_provider_credentials(["kite"], Path(".env"), {"kite": kite})
    assert out == {"kite": False}


def test_unknown_provider_name_is_ignored() -> None:
    assert refresh_provider_credentials(["flattrade"], Path(".env"), {}) == {}
