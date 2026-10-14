"""Pure event -> Telegram HTML string formatting. No I/O, no config, no clock.

Every formatter must degrade gracefully on missing/malformed payload fields —
a formatting bug must never be able to kill the notification worker. Fields are
read via NotificationEvent.get()/getf(), which default rather than raise.
"""

from __future__ import annotations

import html

from src.notifications.events import EventKind, NotificationEvent

_TELEGRAM_MAX_CHARS = 4096

# Real exit `reason` strings produced across order_lifecycle.py, orchestrator.py,
# paper_engine.py, and SmartExitTracker (src/strategies/uss/compiler.py). SmartExitTracker
# reasons ("SL Hit", "Trail SL", "Target Hit", "Reversal", "EOD Exit", "MA Close",
# "Regime Flip", "Time Stop") are already human-readable and pass through unchanged
# via the .get(reason, reason) fallback below — no entry needed for them.
_REASON_LABELS: dict[str, str] = {
    "SL_HIT": "🔴 Stop hit",
    "TARGET_HIT": "🟢 Target hit",
    "SL_GAP_BREACH": "🔴 Stop hit (gap)",
    "TARGET_LIMIT_MISSED": "🟢 Target (limit missed → market)",
    "SL_CHASE_FILLED": "🔴 Stop hit (chased)",
    "TARGET_CHASE_FILLED": "🟢 Target hit (chased)",
    "MIS_FORCE_EXIT": "⏰ MIS flat-by",
    "PARTIAL_FULL": "Partial → full exit",
    "MANUAL_MARKET_EXIT": "🖐 Manual exit",
    "EXIT_WINDOW_FLAT": "⏰ Exit window",
    "EOD_SQUAREOFF": "⏰ EOD square-off",
    "DAILY_LOSS_BREAKER": "🛑 Daily loss breaker",
    "GHOST_TRADE_AUTO_RECONCILED": "🔄 Reconciled (broker-side fill)",
}


def escape_html(text: object) -> str:
    return html.escape(str(text), quote=False)


def mode_tag(is_paper: bool) -> str:
    return "[PAPER]" if is_paper else "[LIVE]"


def mode_segment_tag(ev: NotificationEvent) -> str:
    """ "[LIVE][SWING]" — mode always present, segment appended when the hook
    site set one. Aggregate events (position update, EOD summary) never set
    segment — they group by segment internally instead."""
    tag = mode_tag(ev.is_paper)
    if ev.segment:
        tag += f"[{ev.segment}]"
    return tag


def exit_reason_label(reason: str) -> str:
    """Map a raw reason code to a human label. Unknown/human-readable reasons
    (SmartExitTracker already emits "SL Hit", "Trail SL", etc.) pass through
    verbatim — never raises, never KeyErrors."""
    if not reason:
        return "Exit"
    return _REASON_LABELS.get(reason, reason)


def _clip(text: str) -> str:
    if len(text) <= _TELEGRAM_MAX_CHARS:
        return text
    return text[: _TELEGRAM_MAX_CHARS - 20] + "\n… (truncated)"


def _fmt_money(value: float) -> str:
    return f"₹{value:,.2f}"


def _row_float(row: dict[str, object], key: str, default: float = 0.0) -> float:
    """Typed float accessor for a position-update row (dict[str, object]) —
    mirrors NotificationEvent.getf's tolerate-anything-return-default shape."""
    try:
        raw = row.get(key)
        return default if raw is None else float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def format_entry_fill(ev: NotificationEvent) -> str:
    tag = mode_segment_tag(ev)
    direction = escape_html(ev.get("direction", ""))
    qty = ev.get("qty", ev.get("quantity", ""))
    fill = ev.getf("fill")
    sl = ev.getf("sl")
    target = ev.getf("target")
    product = escape_html(ev.get("product", ""))
    lines = [
        f"{tag} ✅ <b>ENTRY</b> — {escape_html(ev.symbol)} {direction}",
        f"Qty: {escape_html(qty)}  @ {_fmt_money(fill)}" if fill else f"Qty: {escape_html(qty)}",
    ]
    if sl:
        lines.append(f"SL: {_fmt_money(sl)}")
    if target:
        lines.append(f"Target: {_fmt_money(target)}")
    if product:
        lines.append(f"Product: {product}")
    return _clip("\n".join(lines))


def format_entry_rejected(ev: NotificationEvent) -> str:
    tag = mode_segment_tag(ev)
    note = escape_html(ev.get("note") or ev.get("error") or "")
    lines = [f"{tag} ⚠️ <b>ORDER REJECTED</b> — {escape_html(ev.symbol) or ev.agent_trade_id}"]
    if note:
        lines.append(note)
    return _clip("\n".join(lines))


def format_strategy_error(ev: NotificationEvent) -> str:
    strategy = escape_html(ev.get("strategy") or "")
    reason = escape_html(ev.get("reason") or "")
    slot = escape_html(ev.get("slot") or "")
    lines = [f"🛑 <b>LLM ORIGINATION FAILED</b> — {strategy}"]
    if slot:
        lines.append(f"Slot: {slot}")
    if reason:
        lines.append(reason)
    lines.append("No candidates could be evaluated this pass — not a genuine 0-idea decision.")
    return _clip("\n".join(lines))


def format_llm_health(ev: NotificationEvent) -> str:
    trigger = escape_html(str(ev.get("trigger") or ""))
    overall = str(ev.get("overall") or "")
    failures = ev.get("failures") or []
    if overall == "OK":
        return _clip(
            f"✅ <b>LLM HEALTH RECOVERED</b> ({trigger})\n"
            "All providers and gate chains are working."
        )
    icon = "🛑" if overall == "FAIL" else "⚠️"
    lines = [f"{icon} <b>LLM HEALTH {escape_html(overall)}</b> ({trigger})"]
    lines += [f"• {escape_html(str(f))}" for f in list(failures)[:12]]
    return _clip("\n".join(lines))


def format_market_holiday(ev: NotificationEvent) -> str:
    name = escape_html(ev.get("name") or "")
    suffix = f" — {name}" if name else ""
    return _clip(
        f"🏖 <b>NSE HOLIDAY</b>{suffix}\nNo trading today. Scans, LLM calls and updates are paused."
    )


def format_llm_funnel(ev: NotificationEvent) -> str:
    """One step of an LLM idea's journey: where it went, or where it died and why. The symbol
    travels on the event itself (ev.symbol), not in the payload -- the first version read the
    payload and every alert arrived without the stock's name."""
    agent = escape_html(ev.get("agent") or "")
    stage = escape_html(ev.get("stage") or "")
    outcome = str(ev.get("outcome") or "")
    symbol = escape_html(ev.symbol or ev.get("symbol") or "")
    reason = escape_html(ev.get("reason") or "")
    detail = escape_html(ev.get("detail") or "")
    icon = {"placed": "✅", "signal": "➡️", "proposed": "💡"}.get(outcome, "🚫")
    who = f"{symbol} " if symbol else ""
    lines = [f"{icon} <b>{who}{escape_html(outcome.upper())}</b> — {agent}", f"Step: {stage}"]
    if reason:
        lines.append(f"Reason: {reason}")
    if detail:
        lines.append(detail)
    return _clip(chr(10).join(lines))


def format_exit(ev: NotificationEvent) -> str:
    tag = mode_segment_tag(ev)
    reason = exit_reason_label(str(ev.get("reason", "")))
    net_pnl = ev.getf("net_pnl")
    pnl_r = ev.getf("pnl_r")
    entry_px = ev.getf("entry_price")
    exit_px = ev.getf("exit_price")
    qty = ev.get("quantity", "")
    emoji = "🟢" if net_pnl >= 0 else "🔴"
    lines = [
        f"{tag} {emoji} <b>EXIT</b> — {escape_html(ev.symbol)} ({reason})",
    ]
    if entry_px and exit_px:
        lines.append(f"{_fmt_money(entry_px)} → {_fmt_money(exit_px)}  x{escape_html(qty)}")
    pnl_bits = []
    if net_pnl:
        pnl_bits.append(_fmt_money(net_pnl))
    if pnl_r:
        pnl_bits.append(f"{pnl_r:+.2f}R")
    if pnl_bits:
        lines.append("P&L: " + " / ".join(pnl_bits))
    leg_pnls = ev.get("leg_pnls")
    if leg_pnls and len(leg_pnls) > 1:  # multi-leg structure: show where the total came from
        lines.append(
            "Legs: "
            + " · ".join(f"{escape_html(x['leg'])} {_fmt_pnl(float(x['pnl']))}" for x in leg_pnls)
        )
    dropped = ev.get("dropped_count")
    if dropped:
        lines.append(f"⚠ {dropped} notifications dropped earlier")
    return _clip("\n".join(lines))


def format_modify_sl(ev: NotificationEvent) -> str:
    tag = mode_segment_tag(ev)
    new_sl = ev.getf("new_sl")
    fill_price = ev.getf("fill_price")
    is_be = ev.get("is_breakeven", False)
    label = "🔒 Break-even" if is_be else "📈 Stop trailed"
    lines = [f"{tag} {label} — {escape_html(ev.symbol)}"]
    lines.append(
        f"New SL: {_fmt_money(new_sl)}"
        + (f" (entry {_fmt_money(fill_price)})" if fill_price else "")
    )
    trails_since = ev.get("trails_since_update")
    if trails_since:
        lines.append(f"(+{trails_since} trails since last update)")
    return _clip("\n".join(lines))


def _fmt_pnl(value: float) -> str:
    """Signed rupees: +₹1,234.50 / -₹1,735.50."""
    return f"{'+' if value >= 0 else '-'}₹{abs(value):,.2f}"


def format_partial_book(ev: NotificationEvent) -> str:
    """A part of a position is closed while the rest stays open. Generic for every segment: when the hook
    site supplies ``realized_pnl`` (options legs) the REALISED profit or loss is stated first, and
    ``booked_total`` adds the booked-so-far figure for any segment -- a closed leg is booked money and
    must never need the dashboard to be seen."""
    tag = mode_segment_tag(ev)
    if ev.get("realized_pnl") is not None:
        pnl = ev.getf("realized_pnl")
        label = escape_html(ev.get("leg_label") or ev.symbol)
        why = exit_reason_label(str(ev.get("reason", "")))
        lines = [
            f"{tag} {'🟢' if pnl >= 0 else '🔴'} <b>LEG CLOSED</b> — {label} ({why})",
            f"{escape_html(ev.get('side', ''))} {escape_html(ev.get('qty', ''))} · "
            f"{_fmt_money(ev.getf('entry'))} → {_fmt_money(ev.getf('exit'))}",
            f"Realised: <b>{_fmt_pnl(pnl)}</b>",
        ]
        return _clip("\n".join(lines))
    booked_qty = ev.get("booked_qty", "")
    remaining_qty = ev.get("remaining_qty", "")
    price = ev.getf("price")
    lines = [
        f"{tag} 💰 <b>PARTIAL BOOKED</b> — {escape_html(ev.symbol)}",
        f"Sold {escape_html(booked_qty)} @ {_fmt_money(price)}, {escape_html(remaining_qty)} remaining",
    ]
    if ev.get("booked_total") is not None:
        lines.append(f"Booked so far: {_fmt_pnl(ev.getf('booked_total'))}")
    return _clip("\n".join(lines))


def _bare_symbol(symbol: str) -> str:
    """Strip an "NSE:"/"NFO:" exchange prefix before a table truncates the
    symbol to column width — the exchange is almost always NSE and wastes
    space that should go to the actual ticker (found 2026-09-16: "NSE:ANTH",
    "JINDALSA" — the real ticker got cut, not the redundant prefix)."""
    return symbol.split(":")[-1]


def _table_num_compact(value: float, width: int) -> str:
    """Right-aligned number WITHOUT a thousands comma — one row must fit
    SYMBOL/QTY/TARGET/SL/LTP/P&L on a single line (see _position_table), and
    the comma in _table_num cost exactly the character that made a wide
    single-row table wrap on a real phone (found 2026-09-16). At this bot's
    typical price range (double/triple/low-quadruple digits) nothing here
    is ambiguous without the comma."""
    return f"{value:.2f}"[:width].rjust(width)


def _position_table(rows: list[dict[str, object]]) -> str:
    """One <pre> monospace block, ONE line per position: SYMBOL QTY TARGET
    SL LTP P/L, all in a single aligned row (per explicit user request — an
    earlier two-line-per-position layout that split SL/Target onto their own
    indented line was rejected: "why SL is coming below symbol").

    Column widths are deliberately tight (45 chars total) rather than the
    original 59-char six-column attempt, which overflowed this user's
    Telegram client's message width and got hard-wrapped mid-row — "P/L"
    landed on its own line and desynced every following row too (found
    2026-09-16, confirmed via a real screenshot). 45 chars stays under that
    observed ~48-char wrap point while still fitting an 8-char ticker like
    "RELIANCE" in full on one line."""
    header = f"{'SYMBOL':<8} {'QTY':>3} {'TARGET':>7} {'SL':>7} {'LTP':>7} {'P/L':>8}"
    body_lines = [header, "-" * len(header)]
    for row in rows:
        sym = _bare_symbol(str(row.get("symbol", "")))[:8]
        qty = row.get("quantity", "")
        target = _row_float(row, "current_target")
        sl = _row_float(row, "current_sl")
        ltp = _row_float(row, "ltp")
        upl = _row_float(row, "unrealized_pnl")
        body_lines.append(
            f"{sym:<8} {str(qty):>3} {_table_num_compact(target, 7)} "
            f"{_table_num_compact(sl, 7)} {_table_num_compact(ltp, 7)} {upl:>+8.2f}"
        )
    return "<pre>" + escape_html("\n".join(body_lines)) + "</pre>"


_SEGMENT_ORDER = ("INTRADAY", "SWING", "OPTIONS")
_SEGMENT_LABEL: dict[str, str] = {
    "INTRADAY": "Intraday",
    "SWING": "Swing",
    "OPTIONS": "Options",
}


def _options_table(rows: list[dict[str, object]]) -> str:
    """One <pre> monospace block, ONE line per leg — same shape as
    _position_table (one flat header, one row per line, no per-structure
    sub-blocks or repeated headers), per explicit user request to match the
    equity table's style exactly.

    Each leg is its own row (not netted into one aggregate row — a spread's
    protective and income legs move independently, and showing only the
    combined total hid which leg actually drove the P&L; found 2026-09-16,
    a 2-leg bull_put_spread only ever showed as one row). UNDERLYING is
    shown per-row rather than as a block header — multiple legs sharing the
    same underlying/strike proximity are how you tell they're one spread,
    same as _position_table doesn't group equity rows by strategy either."""
    header = f"{'UNDERLYING':<8} {'STRIKE':>6} {'TYP':>3} {'DIR':>4} {'LTP':>7} {'P/L':>8}"
    body_lines = [header, "-" * len(header)]
    for row in rows:
        underlying = _bare_symbol(str(row.get("symbol", "")))[:8]
        strike = _row_float(row, "strike")
        itype = str(row.get("instrument_type", ""))[:3]
        direction = str(row.get("direction", ""))[:4]
        ltp = _row_float(row, "ltp")
        pnl = _row_float(row, "unrealized_pnl")
        body_lines.append(
            f"{underlying:<8} {strike:>6.0f} {itype:>3} {direction:>4} "
            f"{_table_num_compact(ltp, 7)} {pnl:>+8.2f}"
        )
    return "<pre>" + escape_html("\n".join(body_lines)) + "</pre>"


def _segment_group_lines(rows: list[dict[str, object]]) -> list[str]:
    """Rows for ONE mode (paper or live), broken into Intraday/Swing/Options
    sub-sections, each with its own count and table. Per user request:
    separation between paper vs live, then intraday vs swing vs options."""
    lines: list[str] = []
    by_segment: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        seg = str(row.get("segment", "")) or "SWING"
        by_segment.setdefault(seg, []).append(row)
    for seg in _SEGMENT_ORDER:
        group = by_segment.get(seg)
        if not group:
            continue
        label = _SEGMENT_LABEL.get(seg, seg.title())
        # Options rows are one-per-LEG (see build_options_snapshot), so the
        # count here must be distinct structures, not leg rows — otherwise a
        # single 2-leg spread reads as "Options (2)".
        count = (
            len({r.get("position_id") or (r.get("symbol"), r.get("structure_type")) for r in group})
            if seg == "OPTIONS"
            else len(group)
        )
        lines.append(f"  <b>{label}</b> ({count})")
        lines.append(_options_table(group) if seg == "OPTIONS" else _position_table(group))
    return lines


def format_position_update(ev: NotificationEvent) -> str:
    rows: list[dict[str, object]] = ev.get("rows", []) or []
    dropped = ev.get("dropped_count")
    realized_paper = ev.getf("realized_today_paper")
    realized_live = ev.getf("realized_today_live")

    # A mode with zero currently-open positions is NOT the same as a mode
    # with nothing to report — a day where every live trade already closed
    # (net loss, say) has real realized P&L that must still show up here,
    # not get silently skipped because there's no open-position table to go
    # with it (found 2026-09-16: a real -₹1,196.28 live realized loss
    # vanished from the message entirely because LIVE had no open rows).
    if not rows and not realized_paper and not realized_live:
        return _clip("📊 <b>Position update</b> — no open positions")

    paper_rows = [r for r in rows if bool(r.get("is_paper", True))]
    live_rows = [r for r in rows if not bool(r.get("is_paper", True))]

    lines = ["📊 <b>Position update</b>"]
    grand_total = 0.0
    for tag, group, realized, mode_dot in (
        (mode_tag(True), paper_rows, realized_paper, "🔵"),  # paper = blue
        (mode_tag(False), live_rows, realized_live, "🟢"),  # live = green
    ):
        if not group and not realized:
            continue
        unrealized = sum(_row_float(r, "unrealized_pnl") for r in group)
        mode_total = unrealized + realized
        grand_total += mode_total
        # This dot marks WHICH MODE the section below is (paper vs live),
        # not its P&L sign — a red dot here read as "this is a loss" when
        # it actually meant "this is live", which is confusing right where
        # it matters most. Total P/L at the bottom still uses 🟢/🔴 for
        # actual profit/loss.
        open_count = f"{len(group)} open" if group else "no open positions"
        lines.append(f"\n{tag} {mode_dot} {open_count}")
        if group:
            lines.extend(_segment_group_lines(group))
        lines.append(f"  💰 Booked (realized): {_fmt_money(realized)}")
        lines.append(f"  📈 Open (unrealized): {_fmt_money(unrealized)}")
        lines.append(f"  <b>Total today: {_fmt_money(mode_total)}</b>")

    grand_emoji = "🟢" if grand_total >= 0 else "🔴"
    lines.append(f"\n{grand_emoji} <b>Total P/L: {_fmt_money(grand_total)}</b>")
    if dropped:
        lines.append(f"⚠ {dropped} notifications dropped earlier")
    return _clip("\n".join(lines))


def format_eod_summary(ev: NotificationEvent) -> str:
    trades = ev.get("trade_count", 0)
    wins = ev.get("wins", 0)
    losses = ev.get("losses", 0)
    net_pnl = ev.getf("net_pnl")
    net_pnl_r = ev.getf("net_pnl_r")
    best = ev.get("best_symbol", "")
    worst = ev.get("worst_symbol", "")
    win_rate = ev.getf("win_rate")
    open_unrealized = ev.getf("open_unrealized")
    open_count = int(ev.getf("open_count"))
    day_total = net_pnl + open_unrealized
    lines = [
        "📅 <b>End of day summary</b>",
        f"Trades closed: {escape_html(trades)}  (W {escape_html(wins)} / L {escape_html(losses)}, {win_rate:.0f}% win rate)",
        f"💰 Booked (realized): {_fmt_money(net_pnl)}  ({net_pnl_r:+.2f}R equity)",
        f"   Equity {_fmt_money(ev.getf('booked_equity'))} · Options {_fmt_money(ev.getf('booked_options'))}",
        f"📈 Open (unrealized, {open_count} carried overnight): {_fmt_money(open_unrealized)}",
        f"{'🟢' if day_total >= 0 else '🔴'} <b>Total today: {_fmt_money(day_total)}</b>",
    ]
    incomplete = int(ev.getf("options_incomplete"))
    if incomplete:
        lines.append(f"⚠ {incomplete} options close(s) had incomplete P&L (counted as ₹0)")
    if best and worst and best == worst:
        # Only one trade closed today — "Best: X / Worst: X" is the same
        # line twice and reads as broken, not informative.
        lines.append(f"Trade: {escape_html(best)}")
    else:
        if best:
            lines.append(f"Best: {escape_html(best)}")
        if worst:
            lines.append(f"Worst: {escape_html(worst)}")
    return _clip("\n".join(lines))


_DISPATCH = {
    EventKind.ENTRY_FILL: format_entry_fill,
    EventKind.ENTRY_REJECTED: format_entry_rejected,
    EventKind.EXIT: format_exit,
    EventKind.MODIFY_SL: format_modify_sl,
    EventKind.PARTIAL_BOOK: format_partial_book,
    EventKind.POSITION_UPDATE: format_position_update,
    EventKind.EOD_SUMMARY: format_eod_summary,
    EventKind.STRATEGY_ERROR: format_strategy_error,
    EventKind.LLM_HEALTH: format_llm_health,
    EventKind.LLM_FUNNEL: format_llm_funnel,
    EventKind.MARKET_HOLIDAY: format_market_holiday,
}


def format_event(ev: NotificationEvent) -> str:
    fn = _DISPATCH.get(ev.kind)
    if fn is None:
        return _clip(f"{mode_tag(ev.is_paper)} {ev.kind.value} — {escape_html(ev.symbol)}")
    try:
        return fn(ev)
    except Exception:
        # A formatting bug must never kill the worker or drop the notification —
        # fall back to a minimal but still useful message.
        return _clip(f"{mode_tag(ev.is_paper)} {ev.kind.value} — {escape_html(ev.symbol)}")
