"""Orchestrator wiring of the per-day LLM call caps (llm_call_log), intraday tick."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from src import orchestrator as orch

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 24, 11, 30, tzinfo=IST)
CFG = {
    "idea_ttl_minutes": 15,
    "max_trades_per_day": 2,
    "trigger": {"max_passes_per_day": 5, "min_gap_minutes": 30, "min_setup_score": 30},
}


class FakeCallLog:
    def __init__(self, count: int = 0, last: float | None = None, fail: bool = False) -> None:
        self.count, self.last, self.fail, self.recorded = count, last, fail, []

    async def count_today(self, purpose, day):
        if self.fail:
            raise RuntimeError("db down")
        return self.count

    async def last_call_ts(self, purpose, day):
        return self.last

    async def record(self, purpose, day, note=""):
        self.recorded.append((purpose, day, note))


@pytest.fixture
def pass_spy(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(orch, "_run_llm_intraday_pass", spy)
    return spy


async def _tick(repo) -> None:
    shared = {"llm_call_log_repo": repo}
    await orch._llm_intraday_signal_tick(shared, CFG, NOW, MagicMock())


@pytest.mark.asyncio
async def test_baseline_call_is_recorded_before_it_is_made(pass_spy) -> None:
    repo = FakeCallLog(count=0)
    order: list[str] = []
    orig = repo.record

    async def rec(*a, **k):
        order.append("record")
        await orig(*a, **k)

    repo.record = rec

    async def _pass(*a, before_call=None, **k):
        # the real pass books the call only when it is about to dispatch it
        if before_call is None or await before_call():
            order.append("llm_call")

    pass_spy.side_effect = _pass
    await _tick(repo)
    assert order == ["record", "llm_call"]
    assert repo.recorded[0][0] == "intraday"


@pytest.mark.asyncio
async def test_cap_reached_in_db_blocks_the_call_even_in_a_fresh_process(pass_spy) -> None:
    repo = FakeCallLog(count=5, last=NOW.timestamp() - 3600)
    await _tick(repo)
    pass_spy.assert_not_called()
    assert repo.recorded == []


@pytest.mark.asyncio
async def test_restart_shortly_after_a_pass_does_not_refire_baseline(pass_spy) -> None:
    repo = FakeCallLog(count=1, last=NOW.timestamp() - 120)  # fresh process, DB says 1 sent
    await _tick(repo)
    pass_spy.assert_not_called()


@pytest.mark.asyncio
async def test_fails_closed_when_call_count_unreadable(pass_spy) -> None:
    await _tick(FakeCallLog(fail=True))
    pass_spy.assert_not_called()


@pytest.mark.asyncio
async def test_fails_closed_when_no_repo(pass_spy) -> None:
    await _tick(None)
    pass_spy.assert_not_called()


@pytest.mark.asyncio
async def test_does_not_call_llm_if_recording_fails(pass_spy) -> None:
    repo = FakeCallLog(count=0)
    repo.record = AsyncMock(side_effect=RuntimeError("insert failed"))
    llm_called = []

    async def _pass(*a, before_call=None, **k):
        if before_call is None or await before_call():
            llm_called.append(1)

    pass_spy.side_effect = _pass
    await _tick(repo)
    assert llm_called == []  # the cap row could not be written -> no LLM call


@pytest.mark.asyncio
async def test_no_llm_call_while_gate_g3_would_refuse_every_entry(pass_spy, monkeypatch) -> None:
    """The trigger asks G3 itself; if entries are closed it must not call the LLM or burn a
    slot of the daily cap."""
    monkeypatch.setattr(orch, "_g3_entry_window_open", lambda now: False)
    repo = FakeCallLog(count=0)
    await orch._llm_intraday_signal_tick({"llm_call_log_repo": repo}, CFG, NOW, MagicMock())
    pass_spy.assert_not_called()
    assert repo.recorded == []


@pytest.mark.asyncio
async def test_midday_is_open_for_the_agent_no_lunch_restriction(pass_spy) -> None:
    """12:58 used to be a blocked 'lunch' window. It is an ordinary trading minute now."""
    midday = datetime(2026, 9, 24, 12, 58, tzinfo=IST)
    assert orch._g3_entry_window_open(midday) is True


@pytest.mark.asyncio
async def test_a_pass_that_sends_nothing_consumes_no_slot_and_retries_shortly(pass_spy) -> None:
    """2026-09-28: after a 10:10 restart the baseline pass was skipped (`no_intraday_data`) but still
    booked as a pass, which started the 30-minute min_gap -- the morning's first real call slipped to
    12:03. A pass that never dispatches must book nothing and be retried within ~2 minutes."""
    repo = FakeCallLog(count=0)
    shared: dict = {"llm_call_log_repo": repo}

    async def _skipped(*a, before_call=None, **k):
        return None  # e.g. no intraday data yet: returns WITHOUT calling before_call

    pass_spy.side_effect = _skipped
    await orch._llm_intraday_signal_tick(shared, CFG, NOW, MagicMock())
    assert repo.recorded == []  # nothing booked against the cap
    assert shared["llm_intraday_trigger_state"].passes_today == 0
    assert shared["llm_intraday_trigger_state"].last_pass_ts is None

    # inside the retry window the pass is not re-run every 30 s ...
    pass_spy.reset_mock()
    await orch._llm_intraday_signal_tick(shared, CFG, NOW + timedelta(seconds=30), MagicMock())
    pass_spy.assert_not_called()

    # ... and 2+ minutes later the data has arrived: it fires as the baseline (NOT blocked by min_gap)
    async def _ok(*a, before_call=None, **k):
        assert await before_call() is True

    pass_spy.side_effect = _ok
    await orch._llm_intraday_signal_tick(shared, CFG, NOW + timedelta(seconds=130), MagicMock())
    pass_spy.assert_called_once()
    assert len(repo.recorded) == 1
    assert shared["llm_intraday_trigger_state"].passes_today == 1
