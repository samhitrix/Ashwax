"""Unit tests for src/brokers/registry.py and src/brokers/factory.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from src.brokers.base import BrokerInterface
from src.brokers.factory import create_broker
from src.brokers.registry import SUPPORTED_BROKERS, get_adapter_class, list_brokers

# ── registry ───────────────────────────────────────────────────────────────────


def test_list_brokers_includes_kite_and_paper() -> None:
    brokers = list_brokers()
    assert "kite" in brokers
    assert "angel_one" in brokers
    assert "flattrade" in brokers
    assert "paper" in brokers


def test_supported_brokers_is_frozenset() -> None:
    assert isinstance(SUPPORTED_BROKERS, frozenset)
    assert "kite" in SUPPORTED_BROKERS
    assert "angel_one" in SUPPORTED_BROKERS
    assert "flattrade" in SUPPORTED_BROKERS
    assert "paper" in SUPPORTED_BROKERS


def test_get_adapter_class_flattrade() -> None:
    from src.brokers.adapters.flattrade.adapter import FlattradeAdapter

    cls = get_adapter_class("flattrade")
    assert cls is FlattradeAdapter
    assert issubclass(cls, BrokerInterface)


def test_get_adapter_class_kite() -> None:
    from src.brokers.adapters.kite.adapter import KiteAdapter

    assert get_adapter_class("kite") is KiteAdapter


def test_get_adapter_class_angel_one() -> None:
    from src.brokers.adapters.angel_one.adapter import AngelOneAdapter

    assert get_adapter_class("angel_one") is AngelOneAdapter


def test_get_adapter_class_paper() -> None:
    from src.brokers.adapters.paper.adapter import PaperAdapter

    assert get_adapter_class("paper") is PaperAdapter


def test_get_adapter_class_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown broker"):
        get_adapter_class("unicorn")


def test_get_adapter_class_returns_broker_interface_subclass() -> None:
    assert issubclass(get_adapter_class("kite"), BrokerInterface)


# ── factory — paper (default) ──────────────────────────────────────────────────


def test_create_broker_paper_default_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROKER", raising=False)
    fake_market = MagicMock(spec=BrokerInterface)

    broker = create_broker("paper", market_data_broker=fake_market)

    from src.brokers.adapters.paper.adapter import PaperAdapter

    assert isinstance(broker, PaperAdapter)


def test_create_broker_paper_uses_operator_capital(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = {"capital": 150_000.0, "max_risk": 500}
    config_path = tmp_path / "operator.json"
    config_path.write_text(json.dumps(config))

    fake_market = MagicMock(spec=BrokerInterface)

    with patch("src.brokers.factory._CONFIG_PATH", config_path):
        broker = create_broker("paper", market_data_broker=fake_market)

    from src.brokers.adapters.paper.adapter import PaperAdapter

    assert isinstance(broker, PaperAdapter)
    assert broker._cash == 150_000.0


def test_create_broker_paper_capital_override() -> None:
    fake_market = MagicMock(spec=BrokerInterface)
    broker = create_broker("paper", market_data_broker=fake_market, initial_capital=99_000.0)

    from src.brokers.adapters.paper.adapter import PaperAdapter

    assert isinstance(broker, PaperAdapter)
    assert broker._cash == 99_000.0


# ── factory — BROKER env var ───────────────────────────────────────────────────


def test_create_broker_reads_broker_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROKER", "paper")
    fake_market = MagicMock(spec=BrokerInterface)
    broker = create_broker(market_data_broker=fake_market)

    from src.brokers.adapters.paper.adapter import PaperAdapter

    assert isinstance(broker, PaperAdapter)


# ── factory — kite ─────────────────────────────────────────────────────────────


def test_create_broker_kite_sets_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KITE_API_KEY", "test_key")
    monkeypatch.setenv("KITE_SECRET", "test_secret")
    monkeypatch.setenv("KITE_USER_ID", "AB1234")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "my_live_token")

    broker = create_broker("kite")

    from src.brokers.adapters.kite.adapter import KiteAdapter

    assert isinstance(broker, KiteAdapter)
    assert broker._kite.access_token == "my_live_token"


def test_create_broker_kite_no_token_skips_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KITE_API_KEY", "test_key")
    monkeypatch.setenv("KITE_SECRET", "test_secret")
    monkeypatch.setenv("KITE_USER_ID", "AB1234")
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)

    broker = create_broker("kite")

    from src.brokers.adapters.kite.adapter import KiteAdapter

    assert isinstance(broker, KiteAdapter)
    assert broker._kite.access_token is None


# ── factory — angel_one ────────────────────────────────────────────────────────


def test_create_broker_angel_one_sets_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANGEL_ONE_API_KEY", "test_key")
    monkeypatch.setenv("ANGEL_ONE_CLIENT_CODE", "C123456")
    monkeypatch.setenv("ANGEL_ONE_ACCESS_TOKEN", "my_live_jwt")
    monkeypatch.delenv("ANGEL_ONE_REFRESH_TOKEN", raising=False)

    broker = create_broker("angel_one")

    from src.brokers.adapters.angel_one.adapter import AngelOneAdapter

    assert isinstance(broker, AngelOneAdapter)
    assert broker._sc.access_token == "my_live_jwt"


def test_create_broker_angel_one_no_token_skips_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANGEL_ONE_API_KEY", "test_key")
    monkeypatch.setenv("ANGEL_ONE_CLIENT_CODE", "C123456")
    monkeypatch.delenv("ANGEL_ONE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ANGEL_ONE_REFRESH_TOKEN", raising=False)

    broker = create_broker("angel_one")

    from src.brokers.adapters.angel_one.adapter import AngelOneAdapter

    assert isinstance(broker, AngelOneAdapter)
    assert broker._sc.access_token is None


# ── factory — flattrade (F1: session + capability flags only, no trading yet) ─


def _flattrade_env(monkeypatch: pytest.MonkeyPatch, token: str | None) -> None:
    monkeypatch.setenv("FLATTRADE_API_KEY", "test_key")
    monkeypatch.setenv("FLATTRADE_USER_ID", "FZ00000")
    if token is None:
        monkeypatch.delenv("FLATTRADE_ACCESS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("FLATTRADE_ACCESS_TOKEN", token)


def test_create_broker_flattrade_sets_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _flattrade_env(monkeypatch, "my_daily_jkey")

    broker = create_broker("flattrade")

    from src.brokers.adapters.flattrade.adapter import FlattradeAdapter

    assert isinstance(broker, FlattradeAdapter)
    assert broker._auth.access_token == "my_daily_jkey"
    assert broker._client.has_token


def test_create_broker_flattrade_no_token_skips_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _flattrade_env(monkeypatch, None)

    broker = create_broker("flattrade")

    assert broker._auth.access_token == ""  # type: ignore[attr-defined]
    assert not broker._client.has_token  # type: ignore[attr-defined]


def test_flattrade_is_a_market_data_provider() -> None:
    from src.brokers import factory

    # F2: market data is real, so the router may build flattrade (only when credentialed).
    assert factory._PROVIDER_BUILDERS["flattrade"] == "_create_flattrade"
    assert callable(factory._create_flattrade)


async def test_flattrade_adapter_login_is_manual(monkeypatch: pytest.MonkeyPatch) -> None:
    _flattrade_env(monkeypatch, None)
    broker = create_broker("flattrade")
    with pytest.raises(RuntimeError, match="flattrade_auth"):
        await broker.login()
    with pytest.raises(RuntimeError, match="flattrade_auth"):
        await broker.refresh_token()
    status = await broker.get_session_status()
    assert not status.is_valid


def test_flattrade_capability_flags_are_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _flattrade_env(monkeypatch, None)
    broker = create_broker("flattrade")
    assert broker.supports_gtt() is False
    assert broker.supports_bracket_order() is False
    assert broker.supports_cover_order() is False

    caps = json.loads(Path("src/brokers/capability_matrix.json").read_text(encoding="utf-8"))
    ft = caps["flattrade"]
    assert ft["product_codes"] == {"INTRADAY": "I", "DELIVERY": "C", "MARGIN": "M"}
    assert ft["exchanges"] == ["NSE", "BSE", "NFO", "BFO", "MCX"]
    assert ft["gtt_fallback"] == "manual_sl_target_orders"
    assert ft["totp_required"] is False
    assert ft["margin_api_verified"] is False
    assert "UNVERIFIED" in ft["_margin_note"]
    assert "UNVERIFIED" in ft["_note"]


async def test_flattrade_gtt_not_implemented_and_errors_normalise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flattrade_env(monkeypatch, "tok")
    broker = create_broker("flattrade")
    with pytest.raises(NotImplementedError, match="UNVERIFIED"):
        await broker.cancel_gtt("1")
    assert broker.normalize_error({"stat": "Not_Ok", "emsg": "Invalid Input"}).code == "INPUT"
