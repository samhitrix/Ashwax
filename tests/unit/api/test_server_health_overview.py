"""Dashboard API: /api/health_overview — surfaces the live EFFECT of the
health/expectancy learning loop (tier, size multiplier, active pause reason)
per strategy, not just its inputs. This is the endpoint behind the new
"is the learning loop actually doing anything" visibility on the Learning tab.
"""

from __future__ import annotations

from typing import Any

import pytest
from src.api import server


class _FakeIncidentRow(dict):
    def __getitem__(self, key: str) -> Any:
        return dict.__getitem__(self, key)


class _FakeSupervisorRepo:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def open_incidents(self) -> list[dict[str, Any]]:
        return [_FakeIncidentRow(r) for r in self._rows]


@pytest.mark.asyncio
async def test_health_overview_merges_tier_multiplier_and_incident(monkeypatch):
    monkeypatch.setattr(
        server,
        "_read_log",
        lambda name, default=None: {"orb_intraday_smart": "SUSPENDED", "minervini_vcp": "HEALTHY"},
    )
    monkeypatch.setattr(server, "_db_pool", object())  # truthy sentinel

    import src.db.repositories.supervisor as sup_mod

    monkeypatch.setattr(
        sup_mod,
        "SupervisorRepository",
        lambda pool: _FakeSupervisorRepo(
            [
                {
                    "dedup_key": "health_suspended_orb_intraday_smart",
                    "invariant_id": "SH_SUSPENDED",
                    "severity": "CRITICAL",
                    "message": "Strategy orb_intraday_smart SUSPENDED score=12 — auto-paused",
                    "opened_at": "2026-09-03T10:00:00",
                },
                {
                    "dedup_key": "some_unrelated_incident",
                    "invariant_id": "OTHER_THING",
                    "severity": "WARNING",
                    "message": "unrelated",
                    "opened_at": "2026-09-03T10:00:00",
                },
            ]
        ),
    )

    out = await server.get_health_overview()

    by_strategy = {r["strategy"]: r for r in out}
    assert by_strategy["orb_intraday_smart"]["tier"] == "SUSPENDED"
    assert by_strategy["orb_intraday_smart"]["sizeMultiplier"] == 0.0
    assert by_strategy["orb_intraday_smart"]["activeIncident"]["invariantId"] == "SH_SUSPENDED"

    assert by_strategy["minervini_vcp"]["tier"] == "HEALTHY"
    assert by_strategy["minervini_vcp"]["sizeMultiplier"] == 1.0
    assert by_strategy["minervini_vcp"]["activeIncident"] is None

    # Unrelated incident types must never surface here (this endpoint is
    # health/decay-specific, not a general incident feed).
    assert "some_unrelated_incident" not in by_strategy


@pytest.mark.asyncio
async def test_health_overview_no_db_pool_still_returns_tiers(monkeypatch):
    monkeypatch.setattr(
        server, "_read_log", lambda name, default=None: {"minervini_vcp": "CAUTIOUS"}
    )
    monkeypatch.setattr(server, "_db_pool", None)

    out = await server.get_health_overview()

    assert out == [
        {
            "strategy": "minervini_vcp",
            "tier": "CAUTIOUS",
            "sizeMultiplier": 0.75,
            "activeIncident": None,
        }
    ]


@pytest.mark.asyncio
async def test_health_overview_sorted_most_throttled_first(monkeypatch):
    monkeypatch.setattr(
        server,
        "_read_log",
        lambda name, default=None: {"a": "HEALTHY", "b": "SUSPENDED", "c": "DEGRADED"},
    )
    monkeypatch.setattr(server, "_db_pool", None)

    out = await server.get_health_overview()

    assert [r["strategy"] for r in out] == ["b", "c", "a"]
