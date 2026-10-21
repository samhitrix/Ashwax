"""Tests for the factory's market-data router wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from src.brokers import factory
from src.brokers.base import BrokerInterface
from src.market_data.router import MarketDataRouter, NoMarketDataProviderError

CFG = {"market_data": {"providers": ["angel_one", "flattrade", "kite"]}}


@pytest.fixture(autouse=True)
def _reset() -> None:
    factory.reset_market_data_router()
    yield
    factory.reset_market_data_router()


def test_kite_only_machine_resolves_to_kite() -> None:
    fake_kite = MagicMock(spec=BrokerInterface)
    with patch.object(factory, "_create_kite", return_value=fake_kite):
        r = factory.build_market_data_router(CFG, env={"KITE_API_KEY": "k"})
    assert r.names == ["kite"]
    assert r.provider("kite") is fake_kite


def test_angel_first_when_both_eligible() -> None:
    kite, angel = MagicMock(), MagicMock()
    env = {"KITE_API_KEY": "k", "ANGEL_ONE_API_KEY": "a", "ANGEL_ONE_CLIENT_CODE": "c"}
    with (
        patch.object(factory, "_create_kite", return_value=kite),
        patch.object(factory, "_create_angel_one", return_value=angel),
    ):
        r = factory.build_market_data_router(CFG, env=env)
    assert r.names == ["angel_one", "kite"]


def test_kite_less_machine_builds_without_touching_kite() -> None:
    angel = MagicMock()
    env = {"ANGEL_ONE_API_KEY": "a", "ANGEL_ONE_CLIENT_CODE": "c"}
    with (
        patch.object(factory, "_create_kite", side_effect=AssertionError("kite built")),
        patch.object(factory, "_create_angel_one", return_value=angel),
    ):
        r = factory.build_market_data_router(CFG, env=env)
    assert r.names == ["angel_one"]


def test_no_eligible_provider_raises_with_reasons() -> None:
    with pytest.raises(NoMarketDataProviderError) as ei:
        factory.build_market_data_router(CFG, env={})
    msg = str(ei.value)
    assert "angel_one" in msg and "kite" in msg and "flattrade" in msg


def test_get_router_is_cached_and_peek_does_not_build() -> None:
    assert factory.peek_market_data_router() is None
    fake = MagicMock(spec=BrokerInterface)
    with (
        patch.object(factory, "_load_global_config", return_value={}),
        patch.object(factory, "_create_kite", return_value=fake),
        patch.dict("os.environ", {"KITE_API_KEY": "k"}),
    ):
        r1 = factory.get_market_data_router()
        r2 = factory.get_market_data_router()
    assert r1 is r2 and factory.peek_market_data_router() is r1
    assert isinstance(r1, MarketDataRouter)


def test_create_paper_uses_router_when_no_override() -> None:
    from src.brokers.adapters.paper.adapter import PaperAdapter

    router = MagicMock(spec=MarketDataRouter)
    with patch.object(factory, "get_market_data_router", return_value=router):
        paper = factory.create_broker("paper")
    assert isinstance(paper, PaperAdapter) and paper._market is router


def _paper_over(market: object):  # type: ignore[no-untyped-def]
    from src.brokers.adapters.paper.adapter import PaperAdapter

    return PaperAdapter(market_data_broker=market)  # type: ignore[arg-type]


def test_paper_set_access_token_reaches_only_kite_provider_of_router() -> None:
    from src.market_data.provider_config import ProviderConfig

    kite, angel = MagicMock(), MagicMock()
    cfg = ProviderConfig(("angel_one", "kite"), 3, 60.0, {})
    paper = _paper_over(MarketDataRouter({"kite": kite, "angel_one": angel}, cfg))
    paper.set_access_token("KT")
    kite.set_access_token.assert_called_once_with("KT")
    angel.set_access_token.assert_not_called()


def test_paper_set_access_token_on_plain_market_broker_is_unchanged() -> None:
    market = MagicMock(spec=["set_access_token"])
    paper = _paper_over(market)
    paper.set_access_token("KT")
    market.set_access_token.assert_called_once_with("KT")


def test_kite_less_paper_builds_end_to_end() -> None:
    angel = MagicMock(spec=BrokerInterface)
    env = {"ANGEL_ONE_API_KEY": "a", "ANGEL_ONE_CLIENT_CODE": "c"}
    with (
        patch.dict("os.environ", env, clear=True),
        patch.object(factory, "_load_global_config", return_value=CFG),
        patch.object(factory, "_create_kite", side_effect=AssertionError("kite touched")),
        patch.object(factory, "_create_angel_one", return_value=angel),
    ):
        paper = factory.create_broker("paper")
    assert paper._market.names == ["angel_one"]  # type: ignore[attr-defined]


_BOTH_ENV = {"KITE_API_KEY": "k", "ANGEL_ONE_API_KEY": "a", "ANGEL_ONE_CLIENT_CODE": "c"}


def test_warns_when_primary_is_not_kite_and_several_eligible(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        patch.object(factory, "_create_kite", return_value=MagicMock()),
        patch.object(factory, "_create_angel_one", return_value=MagicMock()),
        caplog.at_level("WARNING", logger=factory._log.name),
    ):
        factory.build_market_data_router(CFG, env=_BOTH_ENV)
    msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "market_data_primary_is_not_kite primary=angel_one" in m
        and "also_eligible=['kite']" in m
        and "scripts/compare_provider_data.py" in m
        for m in msgs
    )


def test_no_primary_warning_when_kite_is_first_or_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    kite_first = {"market_data": {"providers": ["kite", "angel_one"]}}
    with (
        patch.object(factory, "_create_kite", return_value=MagicMock()),
        patch.object(factory, "_create_angel_one", return_value=MagicMock()),
        caplog.at_level("WARNING", logger=factory._log.name),
    ):
        factory.build_market_data_router(kite_first, env=_BOTH_ENV)
        factory.build_market_data_router(
            CFG, env={"ANGEL_ONE_API_KEY": "a", "ANGEL_ONE_CLIENT_CODE": "c"}
        )
    assert not any("market_data_primary_is_not_kite" in r.getMessage() for r in caplog.records)


def test_provider_whose_adapter_fails_to_build_is_skipped() -> None:
    angel = MagicMock()
    with (
        patch.object(factory, "_create_kite", side_effect=KeyError("KITE_SECRET")),
        patch.object(factory, "_create_angel_one", return_value=angel),
    ):
        r = factory.build_market_data_router(CFG, env=_BOTH_ENV)
    assert r.names == ["angel_one"]


def test_only_eligible_provider_failing_to_build_raises_with_reason() -> None:
    with (
        patch.object(factory, "_create_kite", side_effect=KeyError("KITE_SECRET")),
        pytest.raises(NoMarketDataProviderError) as ei,
    ):
        factory.build_market_data_router(CFG, env={"KITE_API_KEY": "k"})
    assert "build failed" in str(ei.value) and "KITE_SECRET" in str(ei.value)


# ── flattrade: a real market-data provider since F2, only with credentials ─────

_FT_ENV = {"FLATTRADE_API_KEY": "f", "FLATTRADE_USER_ID": "FZ00000"}


def test_flattrade_only_credentials_build_flattrade() -> None:
    ft = MagicMock(spec=BrokerInterface)
    with patch.object(factory, "_create_flattrade", return_value=ft):
        r = factory.build_market_data_router(CFG, env=_FT_ENV)
    assert r.names == ["flattrade"]
    assert r.provider("flattrade") is ft


def test_kite_plus_flattrade_follows_config_order() -> None:
    kite, ft = MagicMock(spec=BrokerInterface), MagicMock(spec=BrokerInterface)
    with (
        patch.object(factory, "_create_kite", return_value=kite),
        patch.object(factory, "_create_flattrade", return_value=ft),
    ):
        r = factory.build_market_data_router(CFG, env={**_FT_ENV, "KITE_API_KEY": "k"})
    assert r.names == ["flattrade", "kite"]  # CFG lists flattrade before kite


def test_flattrade_without_credentials_is_skipped_and_never_built() -> None:
    kite = MagicMock(spec=BrokerInterface)
    with (
        patch.object(factory, "_create_kite", return_value=kite),
        patch.object(factory, "_create_flattrade", side_effect=AssertionError("built")),
    ):
        r = factory.build_market_data_router(CFG, env={"KITE_API_KEY": "k"})
    assert r.names == ["kite"]


def test_eligible_provider_without_builder_is_skipped_with_reason() -> None:
    with (
        patch.dict(factory._PROVIDER_BUILDERS, clear=True),
        pytest.raises(NoMarketDataProviderError) as ei,
    ):
        factory.build_market_data_router(CFG, env=_FT_ENV)
    assert "no market-data builder yet" in str(ei.value)
