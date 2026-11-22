from __future__ import annotations

import pytest
from src.brokers.stack_keys import (
    LEGACY_LIVE_BROKER,
    live_stacks,
    primary_live_key,
    stack_broker,
    stack_is_paper,
    stack_key,
)


def test_stack_key_paper_ignores_broker() -> None:
    assert stack_key(True) == "paper"
    assert stack_key(True, "angel_one") == "paper"


def test_stack_key_live_names_the_broker_default_legacy() -> None:
    assert stack_key(False, "angel_one") == "live:angel_one"
    assert stack_key(False) == f"live:{LEGACY_LIVE_BROKER}"


def test_stack_is_paper_and_broker_parsing_including_legacy_live_key() -> None:
    assert (
        stack_is_paper("paper") and not stack_is_paper("live:kite") and not stack_is_paper("live")
    )
    assert stack_broker("paper") is None
    assert stack_broker("live") is None  # legacy key used by old tests/fixtures
    assert stack_broker("live:flattrade") == "flattrade"


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValueError):
        stack_broker("weird")
    with pytest.raises(ValueError):
        stack_is_paper("weird")


def test_live_stacks_and_primary_live_key() -> None:
    s = {"paper": 1, "live:kite": 2, "live:angel_one": 3}
    assert live_stacks(s) == {"live:kite": 2, "live:angel_one": 3}
    assert primary_live_key(s) == "live:kite"
    assert primary_live_key({"paper": 1}) is None
    assert primary_live_key({"paper": 1, "live:angel_one": 3}) == "live:angel_one"
    assert primary_live_key({"paper": 1, "live": 9}) == "live"


def test_primary_live_key_prefers_the_given_legacy_broker() -> None:
    s = {"paper": 1, "live:kite": 2, "live:angel_one": 3}
    assert primary_live_key(s, "angel_one") == "live:angel_one"
    assert primary_live_key(s, "kite") == "live:kite"
    # legacy broker has no stack -> previous behaviour
    assert primary_live_key(s, "flattrade") == "live:kite"
    assert primary_live_key(s, None) == "live:kite"
    assert primary_live_key({"paper": 1, "live:angel_one": 3}, "kite") == "live:angel_one"
    assert primary_live_key({"paper": 1, "live": 9}, "angel_one") == "live"
    assert primary_live_key({"paper": 1}, "angel_one") is None
