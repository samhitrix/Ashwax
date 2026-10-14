"""Exit-side tracking for buyer-first (src/options/structure_selector.py)
options_structures rows — Options learning-engine PR5.

Confirmed gap this fixes: options_structures had an insert path only — no
column or code path ever recorded whether a placed structure won or lost
(orchestrator.py's own comment at the entry-notification call site: "No
exit-side tracking exists anywhere for this path"). That silence is also
what blocks PR6's conviction calibration, which needs real outcomes.

Scoped narrowly, per the wiring plan: DETECTION AND RECORDING ONLY, no new
position-management logic.

- SL_FILLED is a real, already-realized close: the stop was placed on the
  primary long leg at entry time (options_executor.py) and the broker closed
  it on its own. This module just notices (via a read-only order-status poll)
  and records the outcome.
- TARGET_REACHED requires actively closing the remaining option leg(s) at
  market (these structures were entered with a market order + a stop, but no
  standing take-profit order) — this IS the natural lifecycle completion of a
  "buy-and-hold to target" structure, not position management/adjustment
  (nothing is rolled, resized, or defended mid-trade; the structure is simply
  closed once its own stated target is reached). `protective_put`'s equity
  leg is NOT reconstructable from options_structures today (never persisted —
  a pre-existing gap, not introduced here) — closing that structure type
  closes only its persisted put leg and logs a prominent warning that the
  equity leg needs separate handling, rather than silently leaving it
  untracked with no signal at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CloseDecision:
    structure_id: str
    exit_reason: str  # "SL_FILLED" | "TARGET_REACHED"
    realized_pnl: float | None
    needs_market_close: bool  # True only for TARGET_REACHED — SL already closed itself


def evaluate_structures(
    open_structures: list[dict[str, Any]],
    current_prices: dict[str, float],
    sl_statuses: dict[str, str],
) -> list[CloseDecision]:
    """Pure function — no I/O. Callers (sweep_structure_exits) do the async
    broker/price fetching and pass the results in here.

    open_structures: options_structures rows (dict-like — asyncpg.Record or
    plain dict), each with at least structure_id, sl_order_id, target, symbol,
    max_loss.
    current_prices: {symbol: current mark/LTP}, already fetched by the caller.
    sl_statuses: {sl_order_id: broker order status string}, already fetched.

    realized_pnl for SL_FILLED is -max_loss (an approximation — the SL is
    placed to cap loss at approximately this level; the exact fill price
    isn't polled by this read-only detector, matching this codebase's existing
    documented approximation pattern for options exits, e.g. seller_desk.py's
    own LTP-captured-before-close realized_pnl). realized_pnl for
    TARGET_REACHED is None here — the caller fills it in from the ACTUAL close
    fill/LTP once it has placed the closing order (evaluate_structures never
    invents a P&L for a position it hasn't actually closed yet).
    """
    decisions: list[CloseDecision] = []
    for s in open_structures:
        structure_id = str(s.get("structure_id", ""))
        if not structure_id:
            continue
        sl_order_id = s.get("sl_order_id")
        if sl_order_id and sl_statuses.get(str(sl_order_id)) == "FILLED":
            max_loss = s.get("max_loss")
            realized = -float(max_loss) if max_loss is not None else None
            decisions.append(CloseDecision(structure_id, "SL_FILLED", realized, False))
            continue

        target = s.get("target")
        symbol = s.get("symbol")
        mark = current_prices.get(str(symbol)) if symbol else None
        if target is not None and mark is not None and float(mark) >= float(target):
            decisions.append(CloseDecision(structure_id, "TARGET_REACHED", None, True))

    return decisions
