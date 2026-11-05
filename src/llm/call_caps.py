"""Pure rules for how many times per day the LLM may be asked (config-driven).

No I/O: the caller supplies the counts (read from llm_call_log) and the config, so the
rules are unit-testable and nothing here can be silently overridden by process state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def swing_max_calls(cfg: dict[str, Any], token_budget: int | None) -> int:
    """Max swing origination calls per day: config `llm_swing.max_calls_per_day`.
    A token budget of exactly 0 (config says swing may not call the LLM) forces 0."""
    if token_budget == 0:
        return 0
    return max(0, int(cfg.get("max_calls_per_day", 2)))


def sync_intraday_state(state: Any, calls_today: int, last_call_ts: float | None) -> None:
    """Raise the in-memory pass counter / last-pass time to what the DB says was really
    sent today. Never lowers them, so an in-flight pass this process already counted is
    kept; after a restart the state is rebuilt from the DB instead of resetting to 0."""
    state.passes_today = max(state.passes_today, calls_today)
    if last_call_ts is not None:
        state.last_pass_ts = max(state.last_pass_ts or 0.0, last_call_ts)


def startup_probe_due(
    last_checked_iso: str | None, now: datetime, min_interval_minutes: float
) -> bool:
    """Whether the boot-time LLM provider probe should run: not if a probe (any trigger)
    already completed within `min_interval_minutes`, so restarts do not each re-ping
    every provider."""
    if not last_checked_iso:
        return True
    try:
        last = datetime.fromisoformat(last_checked_iso)
    except ValueError:
        return True
    return (now - last).total_seconds() >= min_interval_minutes * 60.0
