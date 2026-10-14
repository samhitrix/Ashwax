"""Formatter tests: pure, no I/O. Every EventKind must render, and never raise
regardless of what's in payload."""

from __future__ import annotations

from src.notifications.events import EventKind, NotificationEvent
from src.notifications.formatter import (
    _TELEGRAM_MAX_CHARS,
    escape_html,
    exit_reason_label,
    format_event,
    mode_tag,
)


def test_mode_tag() -> None:
    assert mode_tag(True) == "[PAPER]"
    assert mode_tag(False) == "[LIVE]"


def test_mode_segment_tag_appends_segment_when_set() -> None:
    from src.notifications.events import Segment
    from src.notifications.formatter import mode_segment_tag

    live_swing = NotificationEvent(EventKind.EXIT, False, segment=Segment.SWING)
    assert mode_segment_tag(live_swing) == "[LIVE][SWING]"

    paper_options = NotificationEvent(EventKind.ENTRY_FILL, True, segment=Segment.OPTIONS)
    assert mode_segment_tag(paper_options) == "[PAPER][OPTIONS]"

    no_segment = NotificationEvent(EventKind.EOD_SUMMARY, True)
    assert mode_segment_tag(no_segment) == "[PAPER]"  # no bracket when unset


def test_entry_and_exit_and_modify_and_partial_carry_segment_tag() -> None:
    """Per user request: every per-trade alert, not just the position
    update, must show its segment."""
    from src.notifications.events import Segment
    from src.notifications.formatter import (
        format_entry_fill,
        format_exit,
        format_modify_sl,
        format_partial_book,
    )

    common = {"symbol": "RELIANCE", "is_paper": False, "segment": Segment.SWING}
    assert "[LIVE][SWING]" in format_entry_fill(NotificationEvent(EventKind.ENTRY_FILL, **common))
    assert "[LIVE][SWING]" in format_exit(
        NotificationEvent(EventKind.EXIT, **common, payload={"reason": "SL_HIT"})
    )
    assert "[LIVE][SWING]" in format_modify_sl(
        NotificationEvent(EventKind.MODIFY_SL, **common, payload={"new_sl": 100})
    )
    assert "[LIVE][SWING]" in format_partial_book(
        NotificationEvent(EventKind.PARTIAL_BOOK, **common)
    )


def test_escape_html_neutralizes_special_chars() -> None:
    out = escape_html("M&M <Ltd>")
    assert "<" not in out
    assert "&M" not in out or "&amp;" in out
    assert "&amp;" in out
    assert "&lt;" in out


def test_exit_reason_label_maps_known_codes() -> None:
    assert "Stop" in exit_reason_label("SL_HIT")
    assert "Target" in exit_reason_label("TARGET_HIT")
    assert "Daily loss" in exit_reason_label("DAILY_LOSS_BREAKER")


def test_exit_reason_label_passes_through_unknown_and_human_readable() -> None:
    # SmartExitTracker already emits human-readable reasons like "Trail SL" —
    # must pass through verbatim, never KeyError.
    assert exit_reason_label("Trail SL") == "Trail SL"
    assert exit_reason_label("SOME_FUTURE_REASON") == "SOME_FUTURE_REASON"
    assert exit_reason_label("") == "Exit"


def _all_event_kinds() -> list[NotificationEvent]:
    common = {"agent_trade_id": "T1", "symbol": "RELIANCE & CO"}
    return [
        NotificationEvent(
            EventKind.ENTRY_FILL,
            True,
            **common,
            payload={
                "direction": "BUY",
                "qty": 10,
                "fill": 2500.5,
                "sl": 2450,
                "target": 2600,
                "product": "MIS",
            },
        ),
        NotificationEvent(EventKind.ENTRY_REJECTED, False, **common, payload={"note": "margin<0"}),
        NotificationEvent(
            EventKind.EXIT,
            True,
            **common,
            payload={
                "reason": "SL_HIT",
                "net_pnl": -120.5,
                "pnl_r": -0.8,
                "entry_price": 2500,
                "exit_price": 2450,
                "quantity": 10,
            },
        ),
        NotificationEvent(
            EventKind.MODIFY_SL,
            True,
            **common,
            payload={
                "new_sl": 2480,
                "fill_price": 2500,
                "is_breakeven": True,
            },
        ),
        NotificationEvent(
            EventKind.PARTIAL_BOOK,
            False,
            **common,
            payload={
                "booked_qty": 5,
                "remaining_qty": 5,
                "price": 2550,
            },
        ),
        NotificationEvent(
            EventKind.POSITION_UPDATE,
            True,
            payload={
                "rows": [
                    {
                        "is_paper": True,
                        "symbol": "TCS",
                        "quantity": 10,
                        "fill_price": 3500,
                        "ltp": 3550,
                        "current_sl": 3450,
                        "unrealized_pnl": 500,
                    },
                ]
            },
        ),
        NotificationEvent(EventKind.POSITION_UPDATE, True, payload={"rows": []}),
        NotificationEvent(
            EventKind.EOD_SUMMARY,
            True,
            payload={
                "trade_count": 4,
                "wins": 3,
                "losses": 1,
                "win_rate": 75.0,
                "net_pnl": 1200.0,
                "net_pnl_r": 2.4,
                "best_symbol": "TCS",
                "worst_symbol": "WIPRO",
            },
        ),
        NotificationEvent(
            EventKind.STRATEGY_ERROR,
            True,
            payload={
                "strategy": "llm_intraday_agent",
                "slot": "10:45",
                "reason": "chain exhausted",
            },
        ),
    ]


def test_eod_summary_single_trade_shows_one_trade_line_not_redundant_best_worst() -> None:
    """Regression: with exactly one closed trade, best_symbol == worst_symbol
    (same trade is both), and printing 'Best: X' then 'Worst: X' reads as
    broken, not informative."""
    from src.notifications.formatter import format_eod_summary

    ev = NotificationEvent(
        EventKind.EOD_SUMMARY,
        True,
        payload={
            "trade_count": 1,
            "wins": 0,
            "losses": 1,
            "win_rate": 0.0,
            "net_pnl": -250.0,
            "net_pnl_r": -2.5,
            "best_symbol": "CGCL",
            "worst_symbol": "CGCL",
        },
    )
    text = format_eod_summary(ev)
    assert "Best:" not in text
    assert "Worst:" not in text
    assert "Trade: CGCL" in text
    assert "🔴" in text  # negative net_pnl must show the loss emoji, not 🟢


def test_position_update_renders_aligned_table_with_target_column() -> None:
    """Position update must show symbol/qty/target/SL/LTP/P&L as an aligned
    monospace table, grouped by paper vs live, per user request."""
    from src.notifications.formatter import format_position_update

    ev = NotificationEvent(
        EventKind.POSITION_UPDATE,
        True,
        payload={
            "rows": [
                {
                    "is_paper": True,
                    "symbol": "RELIANCE",
                    "quantity": 10,
                    "fill_price": 2500.0,
                    "ltp": 2550.0,
                    "current_sl": 2470.0,
                    "current_target": 2650.0,
                    "unrealized_pnl": 500.0,
                },
                {
                    "is_paper": False,
                    "symbol": "TCS",
                    "quantity": 5,
                    "fill_price": 3500.0,
                    "ltp": 3480.0,
                    "current_sl": 3550.0,
                    "current_target": 3300.0,
                    "unrealized_pnl": -100.0,
                },
            ]
        },
    )
    text = format_position_update(ev)
    assert "<pre>" in text and "</pre>" in text
    assert "SYMBOL" in text and "LTP" in text and "P/L" in text
    assert "SL" in text and "TARGET" in text  # header columns, same row as SYMBOL
    assert "RELIANCE" in text
    assert "TCS" in text
    assert "2650.00" in text  # target renders (no thousands comma — see
    # _table_num_compact: it costs the width that caused the real wrap bug)
    assert "[PAPER]" in text
    assert "[LIVE]" in text


def test_position_update_groups_by_paper_live_then_segment() -> None:
    """User-requested layout: separation between paper/live, then intraday vs
    swing vs options within each mode."""
    from src.notifications.formatter import format_position_update

    ev = NotificationEvent(
        EventKind.POSITION_UPDATE,
        True,
        payload={
            "realized_today_paper": 100.0,
            "realized_today_live": -50.0,
            "rows": [
                {
                    "is_paper": True,
                    "segment": "INTRADAY",
                    "symbol": "RELIANCE",
                    "quantity": 10,
                    "fill_price": 2500.0,
                    "ltp": 2550.0,
                    "current_sl": 2470.0,
                    "current_target": 2650.0,
                    "unrealized_pnl": 500.0,
                },
                {
                    "is_paper": True,
                    "segment": "SWING",
                    "symbol": "TCS",
                    "quantity": 5,
                    "fill_price": 3500.0,
                    "ltp": 3480.0,
                    "current_sl": 3550.0,
                    "current_target": 3300.0,
                    "unrealized_pnl": -100.0,
                },
                {
                    "is_paper": True,
                    "segment": "OPTIONS",
                    "position_id": "OPT-1",
                    "symbol": "NSE:NIFTY",
                    "structure_type": "iron_condor",
                    "strike": 24000.0,
                    "instrument_type": "PE",
                    "direction": "BUY",
                    "lots": 2,
                    "premium": 100.0,
                    "ltp": 150.0,
                    "unrealized_pnl": 500.0,
                },
                {
                    "is_paper": True,
                    "segment": "OPTIONS",
                    "position_id": "OPT-1",
                    "symbol": "NSE:NIFTY",
                    "structure_type": "iron_condor",
                    "strike": 24200.0,
                    "instrument_type": "PE",
                    "direction": "SELL",
                    "lots": 2,
                    "premium": 80.0,
                    "ltp": 100.0,
                    "unrealized_pnl": -200.0,
                },
                {
                    "is_paper": False,
                    "segment": "SWING",
                    "symbol": "INFY",
                    "quantity": 8,
                    "fill_price": 1500.0,
                    "ltp": 1480.0,
                    "current_sl": 1520.0,
                    "current_target": 1400.0,
                    "unrealized_pnl": -160.0,
                },
            ],
        },
    )
    text = format_position_update(ev)

    # Paper section present with all three segments, live with only swing.
    assert "[PAPER]" in text
    assert "[LIVE]" in text
    assert "Intraday" in text
    assert "Swing" in text
    assert "Options" in text
    assert "RELIANCE" in text and "TCS" in text and "INFY" in text
    # Options shows one row per LEG (not netted into one row per structure),
    # in the SAME flat single-table style as the equity table — one header,
    # one row per leg, no per-structure sub-blocks.
    assert "STRIKE" in text and "DIR" in text
    assert "24000" in text and "24200" in text  # both strikes visible
    assert "Options</b> (1)" in text  # ONE structure, even though it has 2 legs
    # Paper: realized 100 + unrealized (500-100+300=700) = 800
    assert "800.00" in text
    # Live: realized -50 + unrealized -160 = -210
    assert "-210.00" in text
    assert "Total P/L" in text
    assert "590.00" in text  # grand total: 800 + (-210)


def test_position_table_strips_exchange_prefix_before_truncating() -> None:
    """Regression (found 2026-09-16 in a real message): 'NSE:ANTHEM' truncated
    to 8 chars as 'NSE:ANTH' — the exchange prefix ate the width budget that
    should go to the real ticker. Must render 'ANTHEM', not 'NSE:ANTH'."""
    from src.notifications.formatter import format_position_update

    ev = NotificationEvent(
        EventKind.POSITION_UPDATE,
        True,
        payload={
            "rows": [
                {
                    "is_paper": True,
                    "segment": "SWING",
                    "symbol": "NSE:ANTHEM",
                    "quantity": 6,
                    "fill_price": 927.0,
                    "ltp": 908.45,
                    "current_sl": 852.84,
                    "current_target": 1075.32,
                    "unrealized_pnl": -92.09,
                }
            ]
        },
    )
    text = format_position_update(ev)
    assert "ANTHEM" in text
    assert "NSE:ANTH" not in text


def test_options_table_strips_exchange_prefix_too() -> None:
    from src.notifications.formatter import _options_table

    text = _options_table(
        [
            {
                "symbol": "NFO:NIFTY",
                "structure_type": "bull_put_spread",
                "strike": 24000.0,
                "instrument_type": "CE",
                "direction": "SELL",
                "lots": 1,
                "premium": 100.0,
                "ltp": 90.0,
                "unrealized_pnl": 25.0,
            }
        ]
    )
    assert "NIFTY" in text
    assert "NFO:" not in text


def test_position_update_shows_realized_loss_even_with_zero_open_positions() -> None:
    """Regression (found 2026-09-16, real message): a mode with no currently
    open positions still had a real realized loss today (everything that
    traded already closed). That mode's P/L line vanished from the message
    entirely — a real -Rs1,196.28 live loss was invisible. A mode must
    render (with "no open positions" instead of a table) whenever it has
    ANY open rows OR any nonzero realized P&L today."""
    from src.notifications.formatter import format_position_update

    ev = NotificationEvent(
        EventKind.POSITION_UPDATE,
        True,
        payload={
            "rows": [],  # nothing open in EITHER mode
            "realized_today_paper": 0.0,
            "realized_today_live": -1196.28,
        },
    )
    text = format_position_update(ev)
    assert "[LIVE]" in text
    assert "no open positions" in text
    assert "-1,196.28" in text
    assert "Total P/L" in text


def test_position_update_mode_dot_is_identity_not_pnl_sign() -> None:
    """Per user request: the dot next to [PAPER]/[LIVE] marks WHICH MODE the
    section is (paper=blue, live=green), not that mode's P&L sign — a red
    dot there read as "this is a loss" when it meant "this is live". Both
    modes here are LOSING, so a P&L-sign dot would show red for both; the
    identity dot must show blue for paper and green for live regardless."""
    from src.notifications.formatter import format_position_update

    ev = NotificationEvent(
        EventKind.POSITION_UPDATE,
        True,
        payload={
            "rows": [
                {
                    "is_paper": True,
                    "segment": "SWING",
                    "symbol": "TCS",
                    "quantity": 5,
                    "fill_price": 3500.0,
                    "ltp": 3480.0,
                    "current_sl": 3550.0,
                    "current_target": 3300.0,
                    "unrealized_pnl": -100.0,
                },
                {
                    "is_paper": False,
                    "segment": "SWING",
                    "symbol": "INFY",
                    "quantity": 8,
                    "fill_price": 1500.0,
                    "ltp": 1480.0,
                    "current_sl": 1520.0,
                    "current_target": 1400.0,
                    "unrealized_pnl": -160.0,
                },
            ]
        },
    )
    text = format_position_update(ev)
    assert "[PAPER] 🔵" in text
    assert "[LIVE] 🟢" in text
    assert "[PAPER] 🔴" not in text
    assert "[LIVE] 🔴" not in text


def test_position_update_no_rows_and_no_realized_shows_flat_message() -> None:
    """The genuinely-nothing-to-report case (no open positions anywhere, no
    realized P&L today) must still collapse to the short flat message, not
    an empty-but-structured one."""
    from src.notifications.formatter import format_position_update

    ev = NotificationEvent(
        EventKind.POSITION_UPDATE,
        True,
        payload={"rows": [], "realized_today_paper": 0.0, "realized_today_live": 0.0},
    )
    text = format_position_update(ev)
    assert "no open positions" in text
    assert "Total P/L" not in text


def test_position_update_defaults_missing_segment_to_swing() -> None:
    """A row with no segment field (defensive default for any hook site that
    predates segment tagging) must still render, not vanish or crash."""
    from src.notifications.formatter import format_position_update

    ev = NotificationEvent(
        EventKind.POSITION_UPDATE,
        True,
        payload={
            "rows": [
                {
                    "is_paper": True,
                    "symbol": "WIPRO",
                    "quantity": 3,
                    "fill_price": 400.0,
                    "ltp": 410.0,
                    "current_sl": 390.0,
                    "unrealized_pnl": 30.0,
                }
            ]
        },
    )
    text = format_position_update(ev)
    assert "WIPRO" in text
    assert "Swing" in text


def test_every_event_kind_renders_nonempty_and_under_telegram_limit() -> None:
    for ev in _all_event_kinds():
        text = format_event(ev)
        assert isinstance(text, str)
        assert len(text) > 0
        assert len(text) <= _TELEGRAM_MAX_CHARS


def test_format_strategy_error_shows_strategy_slot_and_reason() -> None:
    from src.notifications.formatter import format_strategy_error

    ev = NotificationEvent(
        EventKind.STRATEGY_ERROR,
        True,
        payload={
            "strategy": "llm_intraday_agent",
            "slot": "10:45",
            "reason": "gemini_truncated_response finish_reason=MAX_TOKENS",
        },
    )
    text = format_strategy_error(ev)
    assert "llm_intraday_agent" in text
    assert "10:45" in text
    assert "MAX_TOKENS" in text
    assert "not a genuine 0-idea decision" in text.lower()


def test_format_strategy_error_tolerates_missing_fields() -> None:
    from src.notifications.formatter import format_strategy_error

    text = format_strategy_error(NotificationEvent(EventKind.STRATEGY_ERROR, True))
    assert isinstance(text, str) and len(text) > 0


def test_html_special_chars_in_symbol_are_escaped() -> None:
    ev = NotificationEvent(
        EventKind.ENTRY_FILL,
        True,
        agent_trade_id="T1",
        symbol="M&M<script>",
        payload={"direction": "BUY", "qty": 1, "fill": 100},
    )
    text = format_event(ev)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_format_event_never_raises_on_missing_fields() -> None:
    ev = NotificationEvent(EventKind.EXIT, True, agent_trade_id="T1", symbol="X", payload={})
    text = format_event(ev)
    assert isinstance(text, str)
    assert len(text) > 0


def test_format_event_never_raises_on_malformed_payload_types() -> None:
    ev = NotificationEvent(
        EventKind.MODIFY_SL,
        True,
        agent_trade_id="T1",
        symbol="X",
        payload={"new_sl": "not-a-number", "fill_price": None},
    )
    text = format_event(ev)
    assert isinstance(text, str)
    assert len(text) > 0


def test_unknown_event_kind_falls_back_gracefully() -> None:
    # format_event dispatches via a dict; simulate an unregistered kind by
    # monkeypatching the enum check is unnecessary — the dispatch table
    # already covers every EventKind, so this exercises the fallback branch
    # directly via a kind not in _DISPATCH is impossible with StrEnum here;
    # instead verify the fallback function itself handles a formatter raising.
    from src.notifications import formatter as fmt

    original = fmt._DISPATCH[EventKind.EXIT]
    try:
        fmt._DISPATCH[EventKind.EXIT] = lambda ev: (_ for _ in ()).throw(ValueError("boom"))
        ev = NotificationEvent(EventKind.EXIT, True, agent_trade_id="T1", symbol="X")
        text = format_event(ev)
        assert isinstance(text, str)
        assert len(text) > 0
    finally:
        fmt._DISPATCH[EventKind.EXIT] = original


def test_market_holiday_message_names_the_holiday() -> None:
    from src.notifications.events import EventKind, NotificationEvent
    from src.notifications.formatter import format_event

    text = format_event(
        NotificationEvent(
            kind=EventKind.MARKET_HOLIDAY, is_paper=True, payload={"name": "Mahatma Gandhi Jayanti"}
        )
    )
    assert "NSE HOLIDAY" in text and "Mahatma Gandhi Jayanti" in text


def test_closed_leg_alert_states_the_realised_pnl_first() -> None:
    """A leg closing is booked money. 2026-10-12: the PE leg's stop-out sent nothing, and the dashboard total
    silently dropped its -Rs1,735.50. The alert must lead with the realised P&L and the booked-so-far total."""
    from src.notifications.events import NotificationEvent
    from src.notifications.formatter import format_partial_book

    ev = NotificationEvent(
        EventKind.PARTIAL_BOOK,
        is_paper=True,
        symbol="NFO:NIFTY26O1322300PE",
        segment="OPTIONS",
        payload={
            "leg_label": "NIFTY 22300 PE",
            "side": "SELL",
            "qty": 195,
            "entry": 38.65,
            "exit": 47.55,
            "reason": "SHORT_PREMIUM_SL 23%",
            "realized_pnl": -1735.5,
        },
    )
    text = format_partial_book(ev)
    assert "LEG CLOSED" in text and "NIFTY 22300 PE" in text and "🔴" in text
    assert "Realised: <b>-₹1,735.50</b>" in text
    assert (
        "Still open" not in text and "Booked so far" not in text
    )  # just the realised P&L, nothing else
    assert "38.65" in text and "47.55" in text


def test_exit_alert_shows_every_legs_pnl_for_a_multi_leg_structure() -> None:
    from src.notifications.events import NotificationEvent
    from src.notifications.formatter import format_exit

    ev = NotificationEvent(
        EventKind.EXIT,
        is_paper=True,
        symbol="NSE:NIFTY",
        segment="OPTIONS",
        payload={
            "reason": "SHORT_PREMIUM_TIME_EXIT 15:01",
            "net_pnl": 350.0,
            "leg_pnls": [{"leg": "PE 22300", "pnl": -1735.5}, {"leg": "CE 22600", "pnl": 2086.5}],
        },
    )
    text = format_exit(ev)
    assert "Legs: PE 22300 -₹1,735.50 · CE 22600 +₹2,086.50" in text


def test_single_leg_exit_has_no_redundant_breakdown() -> None:
    from src.notifications.events import NotificationEvent
    from src.notifications.formatter import format_exit

    ev = NotificationEvent(
        EventKind.EXIT,
        is_paper=True,
        symbol="X",
        payload={"net_pnl": 5.0, "leg_pnls": [{"leg": "CE 1", "pnl": 5.0}]},
    )
    assert "Legs:" not in format_exit(ev)
