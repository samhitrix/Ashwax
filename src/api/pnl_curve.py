"""Server-side Intraday P&L curve for the dashboard Overview (display only).

The curve used to live in the browser's memory, so it vanished on every page reload or
launcher restart and had no points while no tab was open. The API process now samples
total P&L once a minute through the trading session and appends it to a per-day file, so
the graph covers the whole day and survives restarts.

``day_pnl`` mirrors the Overview card's rules (dashboard_react/src/store/tradingStore.ts,
refreshData): booked = equity closed today + options closed today + profit already booked
on open swing holds; unrealised = open intraday equity + open options + the rest of the
open swing holds' P&L. Change one side and change the other.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.infrastructure.logger import get_logger
from src.market import sessions
from src.market.calendar import is_trading_day

_log = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
SAMPLE_INTERVAL_S = 60.0
_DEFAULT_DIR = Path(__file__).parent.parent.parent / "logs" / "pnl_curve"

# (net positions, closed-today rows, options rows, swing rows), or None to skip this sample.
Inputs = tuple[list[Any], list[Any], list[Any], list[Any]]
FetchInputs = Callable[[], Awaitable[Inputs | None]]


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_option_row(p: dict[str, Any]) -> bool:
    return str(p.get("exchange", "")).upper() in {"NFO", "BFO"} and str(
        p.get("tradingsymbol", "")
    ).upper().endswith(("CE", "PE"))


def _closed_on(row: dict[str, Any], day: date) -> bool:
    raw = str(row.get("closedAt") or "")
    try:
        return datetime.fromisoformat(raw).astimezone(IST).date() == day
    except ValueError:
        return False


def day_pnl(
    net: list[Any], closed: list[Any], options: list[Any], swing: list[Any], day: date
) -> dict[str, float]:
    """Booked, unrealised and total LIVE P&L across intraday equity, swing holds and options.

    Paper rows (``paper`` / ``isPaper`` true) are excluded: the Overview shows live only."""
    equity_open = sum(
        _num(p.get("pnl"))
        for p in net
        if isinstance(p, dict)
        and not p.get("paper")
        and _num(p.get("quantity")) != 0
        and not _is_option_row(p)
    )
    equity_closed = sum(
        _num(c.get("pnl")) for c in closed if isinstance(c, dict) and not c.get("paper")
    )
    opts = [o for o in options if isinstance(o, dict) and not o.get("isPaper")]
    opt_open = sum(_num(o.get("pnl")) for o in opts if o.get("status") == "OPEN")
    opt_closed = sum(
        _num(o.get("pnl")) for o in opts if o.get("status") == "CLOSED" and _closed_on(o, day)
    )
    open_swing = [
        s for s in swing if isinstance(s, dict) and not s.get("exitTime") and not s.get("paper")
    ]
    swing_total = sum(_num(s.get("pnl")) for s in open_swing)
    swing_booked = sum(_num(s.get("partialPnlBooked")) for s in open_swing)

    booked = equity_closed + opt_closed + swing_booked
    unrealised = equity_open + opt_open + (swing_total - swing_booked)
    return {"booked": booked, "unrealised": unrealised, "pnl": booked + unrealised}


def in_sampling_window(now: datetime) -> bool:
    """Trading day, from the equity open to the F&O close (config/market_hours.json)."""
    now = now.astimezone(IST)
    return is_trading_day(now.date()) and sessions.regular_open(
        "nse_equity"
    ) <= now.time() <= sessions.regular_close("nse_fo")


def record(now: datetime, sample: dict[str, float], base_dir: Path = _DEFAULT_DIR) -> None:
    """Append one sample to today's file (one JSON object per line)."""
    now = now.astimezone(IST)
    base_dir.mkdir(parents=True, exist_ok=True)
    line = {
        "time": now.strftime("%H:%M"),
        "pnl": round(sample["pnl"], 2),
        "booked": round(sample["booked"], 2),
        "unrealised": round(sample["unrealised"], 2),
    }
    with (base_dir / f"{now.date().isoformat()}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def read_curve(day: date, base_dir: Path = _DEFAULT_DIR) -> list[dict[str, Any]]:
    """The day's points in time order, one per minute (the last sample in a minute wins)."""
    path = base_dir / f"{day.isoformat()}.jsonl"
    if not path.exists():
        return []
    by_minute: dict[str, dict[str, Any]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
            by_minute[str(row["time"])] = row
        except (ValueError, KeyError, TypeError):
            continue  # a torn line from a crash mid-write
    return [by_minute[t] for t in sorted(by_minute)]


async def sample_once(
    fetch_inputs: FetchInputs,
    now: datetime | None = None,
    base_dir: Path = _DEFAULT_DIR,
) -> bool:
    """Record one sample if inside the session and the inputs are trustworthy.

    A failed or partial fetch is skipped, never recorded as 0: a fake dip in the curve is
    worse than a one-minute gap."""
    now = now or datetime.now(IST)
    if not in_sampling_window(now):
        return False
    inputs = await fetch_inputs()
    if inputs is None:
        return False
    net, closed, options, swing = inputs
    record(now, day_pnl(net, closed, options, swing, now.astimezone(IST).date()), base_dir)
    return True


async def run(fetch_inputs: FetchInputs, interval: float = SAMPLE_INTERVAL_S) -> None:
    """Sample forever; one bad tick never stops the loop."""
    while True:
        try:
            await sample_once(fetch_inputs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("pnl_curve_sample_failed err=%s", exc)
        await asyncio.sleep(interval)
