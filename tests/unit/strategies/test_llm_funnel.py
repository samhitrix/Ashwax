"""LLM idea funnel: one canonical log line per step, terminal alerts deduped."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from src.notifications.events import EventKind, NotificationEvent
from src.notifications.formatter import format_event
from src.strategies.llm_intraday.funnel import funnel_alert, funnel_log


def test_funnel_log_is_one_greppable_line_with_all_fields() -> None:
    log = MagicMock()
    funnel_log(
        log,
        agent="llm_intraday_agent",
        stage="entry_check",
        outcome="rejected",
        symbol="CIPLA",
        reason="entry_drift",
        drift_pct=0.9,
    )
    level, fmt, msg = log.log.call_args.args
    assert level == logging.INFO and fmt == "llm_funnel %s"
    assert msg == (
        "agent=llm_intraday_agent stage=entry_check outcome=rejected "
        "symbol=CIPLA reason=entry_drift drift_pct=0.9"
    )


def test_funnel_log_uses_dash_for_missing_symbol_and_reason_and_never_raises() -> None:
    log = MagicMock()
    funnel_log(log, agent="a", stage="call_sent", outcome="sent")
    assert "symbol=- reason=-" in log.log.call_args.args[2]
    broken = MagicMock()
    broken.log.side_effect = RuntimeError("handler exploded")
    funnel_log(broken, agent="a", stage="s", outcome="o")  # must not raise


def test_alert_only_for_terminal_outcomes() -> None:
    with patch("src.notifications.notify") as n:
        assert funnel_alert({}, day="d", agent="a", stage="call_sent", outcome="sent") is False
        assert funnel_alert({}, day="d", agent="a", stage="x", outcome="waiting") is False
        assert funnel_alert({}, day="d", agent="a", stage="x", outcome="signal") is False
        n.assert_not_called()
        assert funnel_alert({}, day="d", agent="a", stage="x", outcome="proposed") is True


def test_alert_is_deduped_per_idea_stage_and_reason() -> None:
    shared: dict = {}
    kw = dict(day="2026-09-24", agent="a", stage="entry_check", outcome="expired", symbol="CIPLA")
    with patch("src.notifications.notify") as n:
        assert funnel_alert(shared, reason="entry_drift", **kw) is True
        assert funnel_alert(shared, reason="entry_drift", **kw) is False  # same idea again
        assert funnel_alert(shared, reason="ttl", **kw) is True  # different reason
        assert n.call_count == 2
    assert n.call_args.kwargs["symbol"] == "CIPLA"


def test_alert_never_raises_when_notifier_blows_up() -> None:
    with patch("src.notifications.notify", side_effect=RuntimeError("boom")):
        assert funnel_alert({}, day="d", agent="a", stage="s", outcome="placed") is False


def test_formatter_names_symbol_stage_and_reason() -> None:
    # symbol travels on the event (as notify(symbol=...) sets it), NOT in the payload
    ev = NotificationEvent(
        kind=EventKind.LLM_FUNNEL,
        is_paper=True,
        symbol="TATAINVEST",
        payload={
            "agent": "llm_intraday_agent",
            "stage": "g9",
            "outcome": "blocked",
            "reason": "G9 LLM veto",
            "detail": "reviewer said: weak volume",
        },
    )
    text = format_event(ev)
    assert "TATAINVEST" in text and "g9" in text and "G9 LLM veto" in text and "BLOCKED" in text


def test_alert_carries_the_symbol_end_to_end_into_the_rendered_message() -> None:
    """notify(...) -> event -> formatter: the stock's name must be in what reaches the phone."""
    from src.notifications import notifier as _n

    seen: list[NotificationEvent] = []

    class _Capture:
        def submit(self, ev: NotificationEvent) -> None:
            seen.append(ev)

    with patch.object(_n, "_NOTIFIER", _Capture()):
        funnel_alert(
            {},
            day="d",
            agent="llm_intraday_agent",
            stage="idea_published",
            outcome="proposed",
            symbol="FSL",
            reason="validated",
            detail="SHORT entry=262.70",
        )
    assert "FSL" in format_event(seen[0])
