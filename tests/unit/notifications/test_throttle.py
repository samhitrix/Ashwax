"""Throttler tests. All deterministic via an injected fake clock — no sleeps."""

from __future__ import annotations

import dataclasses

from src.notifications.config import ThrottleConfig
from src.notifications.events import EventKind, NotificationEvent
from src.notifications.throttle import Decision, Throttler


class _FakeClock:
    """Starts at a nonzero epoch, like real time.monotonic() (process uptime,
    never exactly 0.0) — avoids the "0.0 == unset sentinel" falsy-timestamp
    edge case that a clock literally starting at 0 would coincide with."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _cfg(**overrides: object) -> ThrottleConfig:
    base = {
        "modify_min_seconds_per_trade": 90.0,
        "modify_min_r_moved": 0.25,
        "modify_coalesce_seconds": 20.0,
        "modify_max_per_trade_per_day": 12,
        "global_burst": 5,
        "global_refill_seconds": 3.0,
        "dedupe_ttl_seconds": 600.0,
        "startup_quiet_seconds": 0.0,
    }
    base.update(overrides)
    return ThrottleConfig(**base)  # type: ignore[arg-type]


def _modify_ev(new_sl: float, *, is_be: bool = False, direction: str = "BUY") -> NotificationEvent:
    return NotificationEvent(
        kind=EventKind.MODIFY_SL,
        is_paper=True,
        agent_trade_id="T1",
        symbol="RELIANCE",
        payload={
            "new_sl": new_sl,
            "fill_price": 100.0,
            "initial_sl": 90.0,  # R = 10
            "direction": direction,
            "is_breakeven": is_be,
        },
    )


def test_sub_quarter_r_trail_is_suppressed() -> None:
    clock = _FakeClock()
    th = Throttler(_cfg(), _clock=clock)
    ev1 = _modify_ev(91.0)  # first ever -> sent (no last_sent_sl yet)
    assert th.admit(ev1) == Decision.SEND
    th.note_sent(ev1)
    clock.advance(200.0)
    ev2 = _modify_ev(93.0)  # moved 2 < 0.25*10=2.5 -> suppressed
    assert th.admit(ev2) == Decision.SUPPRESS


def test_quarter_r_trail_is_sent() -> None:
    clock = _FakeClock()
    th = Throttler(_cfg(), _clock=clock)
    ev1 = _modify_ev(91.0)
    th.admit(ev1)
    th.note_sent(ev1)
    clock.advance(200.0)
    ev2 = _modify_ev(93.5)  # moved 2.5 == 0.25*10 -> sent
    assert th.admit(ev2) == Decision.SEND


def test_twenty_trails_produce_at_most_eight_messages() -> None:
    """The literal requirement: a stop trailed from -1R to +1R in 20 steps
    (2R total move, 0.1R/step) must produce at most 2R / 0.25R = 8 messages,
    given each step clears the time floor and coalesce window."""
    clock = _FakeClock()
    th = Throttler(_cfg(), _clock=clock)
    fill_price, initial_sl = 100.0, 90.0  # R = 10
    sl = 80.0  # start at -1R
    sent = 0
    for _ in range(20):
        sl += 1.0  # 0.1R per step
        clock.advance(100.0)  # clears both the 90s floor and 20s coalesce
        ev = NotificationEvent(
            kind=EventKind.MODIFY_SL,
            is_paper=True,
            agent_trade_id="T1",
            symbol="RELIANCE",
            payload={
                "new_sl": sl,
                "fill_price": fill_price,
                "initial_sl": initial_sl,
                "direction": "BUY",
            },
        )
        decision = th.admit(ev)
        if decision == Decision.SEND:
            th.note_sent(ev)
            sent += 1
    assert sent <= 8, f"expected <=8 sent messages, got {sent}"


def test_breakeven_bypasses_time_floor() -> None:
    clock = _FakeClock()
    th = Throttler(_cfg(), _clock=clock)
    ev1 = _modify_ev(95.0)
    th.admit(ev1)
    th.note_sent(ev1)
    clock.advance(1.0)  # far inside the 90s floor
    ev2 = _modify_ev(100.0, is_be=True)
    assert th.admit(ev2) == Decision.SEND


def test_partial_book_never_throttled() -> None:
    clock = _FakeClock()
    th = Throttler(_cfg(), _clock=clock)
    ev = NotificationEvent(
        kind=EventKind.PARTIAL_BOOK,
        is_paper=True,
        agent_trade_id="T1",
        symbol="RELIANCE",
        payload={},
    )
    assert th.admit(ev) == Decision.SEND
    clock.advance(0.001)
    assert th.admit(dataclasses.replace(ev)) != Decision.SUPPRESS


def test_second_modify_within_coalesce_window_replaces_first() -> None:
    clock = _FakeClock()
    th = Throttler(_cfg(modify_min_r_moved=0.0, modify_min_seconds_per_trade=0.0), _clock=clock)
    ev1 = _modify_ev(91.0)
    th.admit(ev1)
    th.note_sent(ev1)
    clock.advance(5.0)  # inside the 20s coalesce window
    ev2 = _modify_ev(92.0)
    assert th.admit(ev2) == Decision.COALESCE


def test_per_trade_daily_cap_suppresses_then_resets_at_rollover() -> None:
    clock = _FakeClock()
    th = Throttler(
        _cfg(modify_min_r_moved=0.0, modify_min_seconds_per_trade=0.0, modify_coalesce_seconds=0.0),
        _clock=clock,
    )
    sl = 91.0
    for _ in range(12):
        clock.advance(1000.0)
        ev = _modify_ev(sl)
        assert th.admit(ev) == Decision.SEND
        th.note_sent(ev)
        sl += 1.0
    clock.advance(1000.0)
    ev13 = _modify_ev(sl)
    assert th.admit(ev13) == Decision.SUPPRESS

    # Simulate a day rollover by resetting the trade's day_key directly —
    # _maybe_roll_day compares against datetime.date.today(), which a fake
    # monotonic clock cannot move; this exercises the same reset path.
    th._trades["T1"].day_key = "2000-01-01"
    ev_next_day = _modify_ev(sl)
    assert th.admit(ev_next_day) == Decision.SEND


def test_token_bucket_refills_and_prioritises_critical() -> None:
    clock = _FakeClock()
    th = Throttler(_cfg(global_burst=2, global_refill_seconds=3.0), _clock=clock)
    assert th.take_token() == 0.0
    assert th.take_token() == 0.0
    wait = th.take_token()
    assert wait > 0.0

    critical_ev = NotificationEvent(
        kind=EventKind.ENTRY_FILL, is_paper=True, agent_trade_id="T2", symbol="TCS"
    )
    # CRITICAL priority always SENDs from admit() regardless of bucket state —
    # the bucket only governs pacing in the worker, not admission.
    assert th.admit(critical_ev) == Decision.SEND


def test_dedupe_ttl_expires() -> None:
    clock = _FakeClock()
    th = Throttler(_cfg(dedupe_ttl_seconds=60.0), _clock=clock)
    th.note_dedupe("entry:T1")
    assert th.is_duplicate("entry:T1") is True
    clock.advance(61.0)
    assert th.is_duplicate("entry:T1") is False


def test_startup_quiet_window() -> None:
    clock = _FakeClock()
    th = Throttler(_cfg(startup_quiet_seconds=60.0), _clock=clock)
    assert th.in_startup_quiet_window() is True
    clock.advance(61.0)
    assert th.in_startup_quiet_window() is False
