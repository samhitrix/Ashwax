"""Tests for the 5-tier emergency alert engine."""

from __future__ import annotations

from src.infrastructure.alert_system import (
    TIER_POLICY,
    AlertManager,
    AlertTier,
)


def test_raise_appears_in_active_with_dashboard_shape() -> None:
    mgr = AlertManager()
    a = mgr.raise_alert(AlertTier.WARNING, "VIX elevated", source="circuit_breaker")
    assert mgr.active() == [a]
    item = a.to_item()
    # matches dashboard_react AlertItem contract exactly
    assert set(item) == {"id", "ts", "tier", "message", "acknowledged"}
    assert item["tier"] == "WARNING"
    assert item["acknowledged"] is False


def test_acknowledge_dismissible_tier() -> None:
    mgr = AlertManager()
    a = mgr.raise_alert(AlertTier.CRITICAL, "Broker session lost")
    assert mgr.acknowledge(a.id) is True
    assert a.acknowledged is True
    assert mgr.unacknowledged() == []


def test_emergency_cannot_be_acknowledged() -> None:
    mgr = AlertManager()
    a = mgr.raise_alert(AlertTier.EMERGENCY, "Floor approaching")
    assert mgr.acknowledge(a.id) is False
    assert a.acknowledged is False
    assert mgr.unacknowledged() == [a]  # still screaming for attention


def test_resolve_removes_from_active() -> None:
    mgr = AlertManager()
    a = mgr.raise_alert(AlertTier.ALERT, "Strategy DEGRADED")
    assert mgr.resolve(a.id) is True
    assert mgr.active() == []


def test_highest_active_tier() -> None:
    mgr = AlertManager()
    mgr.raise_alert(AlertTier.INFO, "ok")
    mgr.raise_alert(AlertTier.EMERGENCY, "floor")
    mgr.raise_alert(AlertTier.WARNING, "vix")
    assert mgr.highest_active_tier() is AlertTier.EMERGENCY


def test_highest_active_tier_none_when_quiet() -> None:
    assert AlertManager().highest_active_tier() is None


def test_tier_policy_matches_spec() -> None:
    assert TIER_POLICY[AlertTier.INFO].push is False
    assert TIER_POLICY[AlertTier.CRITICAL].push is True
    assert TIER_POLICY[AlertTier.EMERGENCY].external is True
    assert TIER_POLICY[AlertTier.EMERGENCY].dismissible is False
    # all five tiers have a policy
    assert set(TIER_POLICY) == set(AlertTier)


def test_acknowledge_unknown_id_returns_false() -> None:
    assert AlertManager().acknowledge("nope") is False


def test_on_change_hook_fires_for_every_raise() -> None:
    """Supervisor wires on_change = self._flush_alerts so every raise_alert() call —
    including the ones order_lifecycle.py makes directly into this same shared
    instance — persists to logs/alerts.json immediately, not only when Supervisor's
    own invariant loop happens to fire next (2026-09-07: a real CGCL "position
    unprotected" CRITICAL sat unflushed for 12+ hours because of this gap)."""
    mgr = AlertManager()
    calls = 0

    def _flush() -> None:
        nonlocal calls
        calls += 1

    mgr.on_change = _flush
    mgr.raise_alert(AlertTier.CRITICAL, "deferred_protection_failed CGCL: position unprotected")
    mgr.raise_alert(AlertTier.CRITICAL, "GTT re-arm failed for NSE:CGCL: tick size")
    assert calls == 2


def test_on_change_fires_on_resolve_too() -> None:
    """A fixed alert disappearing off the dashboard needs the same flush as a new
    one appearing — resolve() must trigger on_change exactly like raise_alert()."""
    mgr = AlertManager()
    calls = 0

    def _flush() -> None:
        nonlocal calls
        calls += 1

    a = mgr.raise_alert(AlertTier.CRITICAL, "gtt missing")
    mgr.on_change = _flush
    assert mgr.resolve(a.id) is True
    assert calls == 1


def test_resolve_by_source_clears_only_matching_active_alerts() -> None:
    mgr = AlertManager()
    a1 = mgr.raise_alert(AlertTier.CRITICAL, "gtt missing", source="TRD-CGCL")
    mgr.raise_alert(AlertTier.CRITICAL, "unrelated", source="TRD-OTHER")

    resolved = mgr.resolve_by_source("TRD-CGCL")

    assert resolved == 1
    assert a1.id not in [a.id for a in mgr.active()]
    assert len(mgr.active()) == 1


def test_resolve_by_source_flushes_once_for_multiple_matches() -> None:
    mgr = AlertManager()
    calls = 0

    def _flush() -> None:
        nonlocal calls
        calls += 1

    mgr.raise_alert(AlertTier.CRITICAL, "first", source="TRD-CGCL")
    mgr.raise_alert(AlertTier.CRITICAL, "second", source="TRD-CGCL")
    mgr.on_change = _flush

    resolved = mgr.resolve_by_source("TRD-CGCL")

    assert resolved == 2
    assert calls == 1


def test_resolve_by_source_no_match_does_not_flush() -> None:
    mgr = AlertManager()
    calls = 0

    def _flush() -> None:
        nonlocal calls
        calls += 1

    mgr.on_change = _flush
    assert mgr.resolve_by_source("nothing-active") == 0
    assert calls == 0


def test_on_change_unset_by_default() -> None:
    """No hook wired means raise_alert must still work exactly as before."""
    mgr = AlertManager()
    a = mgr.raise_alert(AlertTier.WARNING, "no hook wired")
    assert mgr.active() == [a]
