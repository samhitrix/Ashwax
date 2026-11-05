"""Unit tests for src/market_data/provider_config.py."""

from __future__ import annotations

import pytest
from src.market_data.provider_config import (
    ProviderConfig,
    parse_provider_config,
    resolve_eligible,
)

REGISTERED = ["kite", "angel_one", "flattrade", "paper"]


def test_missing_block_defaults_to_kite_only() -> None:
    cfg = parse_provider_config({})
    assert cfg.providers == ("kite",)
    assert cfg.max_consecutive_failures == 3
    assert cfg.cooldown_seconds == 300.0
    assert cfg.per_use == {}


def test_parses_order_failover_and_per_use() -> None:
    cfg = parse_provider_config(
        {
            "market_data": {
                "providers": ["angel_one", "flattrade", "kite"],
                "failover": {"max_consecutive_failures": 5, "cooldown_seconds": 60},
                "per_use": {"options_chain": ["kite", "angel_one"]},
            }
        }
    )
    assert cfg.providers == ("angel_one", "flattrade", "kite")
    assert cfg.max_consecutive_failures == 5
    assert cfg.cooldown_seconds == 60.0
    assert cfg.per_use == {"options_chain": ("kite", "angel_one")}


@pytest.mark.parametrize(
    "bad",
    [
        {"market_data": {"providers": []}},
        {"market_data": {"providers": "kite"}},
        {"market_data": {"providers": ["kite", 5]}},
        {"market_data": {"providers": ["kite"], "per_use": {"nonsense": ["kite"]}}},
        {"market_data": {"providers": ["kite"], "failover": {"max_consecutive_failures": 0}}},
    ],
)
def test_invalid_config_raises_value_error(bad: dict) -> None:
    with pytest.raises(ValueError):
        parse_provider_config(bad)


def test_eligibility_requires_credentials_and_registration() -> None:
    cfg = ProviderConfig(("angel_one", "flattrade", "kite"), 3, 300.0, {})
    env = {"KITE_API_KEY": "k"}  # no Angel creds
    eligible, skipped = resolve_eligible(cfg, REGISTERED, env)
    assert eligible == ["kite"]
    assert "angel_one" in skipped and "credentials" in skipped["angel_one"]
    # flattrade is registered with a credential rule, so it is skipped for missing creds
    assert "flattrade" in skipped and "credentials missing" in skipped["flattrade"]
    assert "FLATTRADE_API_KEY" in skipped["flattrade"]


def test_unregistered_provider_is_skipped_as_not_registered() -> None:
    cfg = ProviderConfig(("upstox", "kite"), 3, 300.0, {})
    eligible, skipped = resolve_eligible(cfg, REGISTERED, {"KITE_API_KEY": "k"})
    assert eligible == ["kite"]
    assert "not registered" in skipped["upstox"]


def test_flattrade_eligible_with_key_and_user_id() -> None:
    cfg = ProviderConfig(("flattrade", "kite"), 3, 300.0, {})
    env = {"FLATTRADE_API_KEY": "f", "FLATTRADE_USER_ID": "FZ00000"}
    eligible, skipped = resolve_eligible(cfg, REGISTERED, env)
    assert eligible == ["flattrade"]
    assert "credentials" in skipped["kite"]


def test_flattrade_rule_matches_real_registry() -> None:
    from src.brokers.registry import list_brokers
    from src.market_data.provider_config import CREDENTIAL_ENV

    assert "flattrade" in list_brokers()
    assert CREDENTIAL_ENV["flattrade"] == ("FLATTRADE_API_KEY", "FLATTRADE_USER_ID")


def test_eligibility_preserves_config_order() -> None:
    cfg = ProviderConfig(("angel_one", "kite"), 3, 300.0, {})
    env = {"KITE_API_KEY": "k", "ANGEL_ONE_API_KEY": "a", "ANGEL_ONE_CLIENT_CODE": "c"}
    eligible, skipped = resolve_eligible(cfg, REGISTERED, env)
    assert eligible == ["angel_one", "kite"]
    assert skipped == {}


def test_registered_provider_without_credential_rule_is_skipped() -> None:
    cfg = ProviderConfig(("paper",), 3, 300.0, {})
    eligible, skipped = resolve_eligible(cfg, REGISTERED, {})
    assert eligible == []
    assert "no credential rule" in skipped["paper"]


def test_shipped_global_json_is_valid_and_has_owner_order() -> None:
    import json
    from pathlib import Path

    cfg = json.loads(Path("config/global.json").read_text(encoding="utf-8"))
    parsed = parse_provider_config(cfg)
    assert parsed.providers == ("kite", "angel_one", "flattrade")
