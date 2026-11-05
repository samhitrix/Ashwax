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
    # Non-LLM strategies (e.g. the options short-premium missed-entry report) pass their own title.
    title = escape_html(ev.get("title") or "")
    lines = [f"🛑 <b>{title or 'LLM ORIGINATION FAILED'}</b> — {strategy}"]
    if slot:
        lines.append(f"Slot: {slot}")
    if reason:
        lines.append(reason)
    if not title:
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
    """Signed rupees: +₹N / -₹N."""
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


def _table_px(value: float | None, width: int = 6) -> str:
    """Right-aligned premium, 2 decimals, fewer if that overflows `width`, "-" for None
    (no such level). Keeps the options row at 45 chars -- see _options_table."""
    if value is None:
        return "-".rjust(width)
    for decimals in (2, 1, 0):
        text = f"{value:.{decimals}f}"
        if len(text) <= width:
            break
    return text[:width].rjust(width)


def position_count(rows: list[dict[str, object]]) -> int:
    """Open POSITIONS in `rows`: equity rows count one each, options rows are one per LEG
    (see build_options_snapshot) so they count once per structure -- a 4-leg condor is
    one position, not four."""
    equity = [r for r in rows if r.get("segment") != "OPTIONS"]
    options = {
        r.get("position_id") or (r.get("symbol"), r.get("structure_type"))
        for r in rows
        if r.get("segment") == "OPTIONS"
    }
    return len(equity) + len(options)


def _options_table(rows: list[dict[str, object]]) -> str:
    """One <pre> monospace block, ONE line per leg — same shape as
    _position_table (one flat header, one row per line, no per-structure
    sub-blocks or repeated headers), per explicit user request to match the
    equity table's style exactly.

    Each leg is its own row (not netted into one aggregate row — a spread's
    protective and income legs move independently, and showing only the
    combined total hid which leg actually drove the P&L; found earlier,
    a 2-leg bull_put_spread only ever showed as one row). UNDERLYING is
    shown per-row rather than as a block header — multiple legs sharing the
    same underlying/strike proximity are how you tell they're one spread,
    same as _position_table doesn't group equity rows by strategy either.

    SL/TGT are premiums on the same row as LTP (user request 2026-10-30: in the
    table, never wrapped onto a new line), so the row is squeezed to the equity
    table's 45 chars: 6-char underlying, "PE S" for type+direction, whole-rupee
    P/L. A leg with no stop of its own shows "-"; its structure's rupee target/stop
    goes on one line under that structure's legs instead.

    Legs are grouped by strategy (user request 2026-11-01): a bold strategy name, then
    that strategy's own table. Telegram allows no bold inside <pre> and has no text
    colour, so the header row stays plain and P/L sign is shown as a 🟢/🔴 dot
    (replacing the space before the number, so the row stays within ~46 columns)."""
    header = f"{'SYM':<6} {'STRK':>5} {'OPT':>4} {'LTP':>6} {'SL':>6} {'TGT':>6} {'P/L':>7}"
    by_strategy: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_strategy.setdefault(str(row.get("structure_type", "") or "?"), []).append(row)
    blocks: list[str] = []
    for strategy, group in by_strategy.items():
        body_lines = [header, "-" * len(header)]
        footers: dict[object, str] = {}
        for row in group:
            underlying = _bare_symbol(str(row.get("symbol", "")))[:6]
            strike = _row_float(row, "strike")
            opt = f"{str(row.get('instrument_type', ''))[:2]} {str(row.get('direction', ''))[:1]}"
            ltp = _row_float(row, "ltp")
            pnl = _row_float(row, "unrealized_pnl")
            sl = row.get("leg_sl")
            tgt = row.get("leg_target")
            body_lines.append(
                f"{underlying:<6} {strike:>5.0f} {opt:>4} {_table_px(ltp)} "
                f"{_table_px(sl if isinstance(sl, int | float) else None)} "
                f"{_table_px(tgt if isinstance(tgt, int | float) else None)} "
                f"{_pnl_dot(pnl)}{pnl:>+5.0f}"
            )
            pos_target, pos_stop = row.get("pos_target"), row.get("pos_stop")
            if isinstance(pos_target, int | float) or isinstance(pos_stop, int | float):
                parts = []
                if isinstance(pos_target, int | float):
                    parts.append(f"target {_fmt_money(pos_target)}")
                if isinstance(pos_stop, int | float):
                    parts.append(f"stop {_fmt_money(-pos_stop)}")
                key = row.get("position_id") or (row.get("symbol"), strategy)
                footers[key] = f"  {underlying} {' · '.join(parts)}"
        body_lines.extend(footers.values())
        blocks.append(f"  <b>▸ {escape_html(strategy)}</b>")
        blocks.append("<pre>" + escape_html("\n".join(body_lines)) + "</pre>")
    return "\n".join(blocks)


def _pnl_dot(value: float) -> str:
    """🟢 profit / 🔴 loss / ⚪ flat -- Telegram has no text colour, so this is the colour."""
    if round(value) > 0:
        return "🟢"
    if round(value) < 0:
        return "🔴"
    return "⚪"


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
        count = position_count(group)
        lines.append(f"  <b>{label}</b> ({count})")
        lines.append(_options_table(group) if seg == "OPTIONS" else _position_table(group))
    return lines


def _broker_group_lines(rows: list[dict[str, object]]) -> list[str]:
    """One mode's rows. With open positions on two or more brokers each broker gets a
    header (rows with no broker, e.g. the options desk, come first, unlabelled); with at most
    one broker the output is exactly _segment_group_lines, unchanged."""
    brokers = sorted({str(r["broker"]) for r in rows if r.get("broker")})
    if len(brokers) < 2:
        return _segment_group_lines(rows)
    lines = (
        _segment_group_lines([r for r in rows if not r.get("broker")])
        if any(not r.get("broker") for r in rows)
        else []
    )
    for broker in brokers:
        lines.append(f"  🏦 <b>{escape_html(broker)}</b>")
        lines.extend(_segment_group_lines([r for r in rows if r.get("broker") == broker]))
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
    # with it (found earlier: a real -₹N live realized loss
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
        open_count = f"{position_count(group)} open" if group else "no open positions"
        lines.append(f"\n{tag} {mode_dot} {open_count}")
        if group:
            lines.extend(_broker_group_lines(group))
        lines.append(f"  💰 Booked (realized): {_fmt_money(realized)}")
        lines.append(f"  📈 Open (unrealized): {_fmt_money(unrealized)}")
        lines.append(f"  {_pnl_dot(mode_total)} <b>Total today: {_fmt_money(mode_total)}</b>")

    grand_emoji = _pnl_dot(grand_total)
    lines.append(f"\n{grand_emoji} <b>Total P/L: {_fmt_money(grand_total)}</b>")
    if dropped:
        lines.append(f"⚠ {dropped} notifications dropped earlier")
    return _clip("\n".join(lines))


def _eod_mode_lines(stats: dict[str, object]) -> tuple[list[str], float]:
    """One mode's EOD block (indented under its [PAPER]/[LIVE] header) and its day total."""

    def num(key: str) -> float:
        try:
            return float(stats.get(key) or 0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    trades = int(num("trade_count"))
    open_count = int(num("open_count"))
    net_pnl = num("net_pnl")
    open_unrealized = num("open_unrealized")
    day_total = net_pnl + open_unrealized
    if not trades and not open_count and not net_pnl:
        return ["  no trades, nothing open"], 0.0
    lines = [
        f"  Trades closed: {trades}  (W {int(num('wins'))} / L {int(num('losses'))}, "
        f"{num('win_rate'):.0f}% win rate)",
        f"  💰 Booked (realized): {_fmt_money(net_pnl)}  ({num('net_pnl_r'):+.2f}R equity)",
        f"     Equity {_fmt_money(num('booked_equity'))} · Options {_fmt_money(num('booked_options'))}",
    ]
    by_strategy = stats.get("options_by_strategy")
    if isinstance(by_strategy, dict):
        # One line per options strategy (user request 2026-11-01), biggest win first.
        for strategy, pnl in sorted(by_strategy.items(), key=lambda kv: -float(kv[1])):
            lines.append(f"       ▸ {escape_html(strategy)} {_fmt_money(float(pnl))}")
    lines += [
        f"  📈 Open (unrealized, {open_count} carried overnight): {_fmt_money(open_unrealized)}",
        f"  {_pnl_dot(day_total)} <b>Total today: {_fmt_money(day_total)}</b>",
    ]
    incomplete = int(num("options_incomplete"))
    if incomplete:
        lines.append(f"  ⚠ {incomplete} options close(s) had incomplete P&L (counted as ₹0)")
    best = str(stats.get("best_symbol") or "")
    worst = str(stats.get("worst_symbol") or "")
    if best and worst and best == worst:
        # Only one trade closed — "Best: X / Worst: X" is the same line twice
        # and reads as broken, not informative.
        lines.append(f"  Trade: {escape_html(best)}")
    else:
        if best:
            lines.append(f"  Best: {escape_html(best)}")
        if worst:
            lines.append(f"  Worst: {escape_html(worst)}")
    return lines, day_total


def format_eod_summary(ev: NotificationEvent) -> str:
    """Paper and live as separate blocks (user request 2026-10-30: the single combined
    block hid which money was real), same tags/dots as the Position update, then the
    grand total."""
    lines = ["📅 <b>End of day summary</b>"]
    grand_total = 0.0
    for key, is_paper, mode_dot in (("paper", True, "🔵"), ("live", False, "🟢")):
        stats = ev.get(key) or {}
        block, total = _eod_mode_lines(stats)
        grand_total += total
        lines.append(f"\n{mode_tag(is_paper)} {mode_dot}")
        lines.extend(block)
    lines.append(f"\n{_pnl_dot(grand_total)} <b>Total P/L: {_fmt_money(grand_total)}</b>")
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
