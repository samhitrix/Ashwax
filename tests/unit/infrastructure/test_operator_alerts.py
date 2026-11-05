"""operator_alerts: one call -> dashboard Alerts tab + Telegram, deduped, never raises."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from src.infrastructure import operator_alerts as oa
from src.infrastructure.alert_system import AlertManager, AlertTier


@pytest.fixture(autouse=True)
def _clean():
    oa._reset_for_testing()
    yield
    oa._reset_for_testing()


def test_raises_on_dashboard_and_pushes_to_telegram() -> None:
    mgr = AlertManager()
    oa.configure(mgr)
    with patch("src.notifications.notify") as n:
        assert oa.raise_alert("provider anthropic failed", source="llm_failover:swing:anthropic")
    active = mgr.active()
    assert len(active) == 1 and active[0].tier == AlertTier.CRITICAL
    assert "provider anthropic failed" in active[0].message
    assert n.call_args.kwargs["failures"] == ["provider anthropic failed"]


def test_same_source_is_deduped_but_other_sources_are_not() -> None:
    mgr = AlertManager()
    oa.configure(mgr)
    with patch("src.notifications.notify"):
        assert oa.raise_alert("a", source="s1") is True
        assert oa.raise_alert("a again", source="s1") is False
        assert oa.raise_alert("b", source="s2") is True
    assert len(mgr.active()) == 2


def test_push_can_be_disabled_and_works_without_a_manager() -> None:
    with patch("src.notifications.notify") as n:
        assert oa.raise_alert("x", source="s", push=False) is True
    n.assert_not_called()


def test_never_raises_even_when_everything_is_broken() -> None:
    broken = MagicMock()
    broken.raise_alert.side_effect = RuntimeError("boom")
    oa.configure(broken)
    assert oa.raise_alert("x", source="s") is False
