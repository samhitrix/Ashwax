"""
Price Truth Engine — 3-phase fill price verification.

Phase 1 (On fill): Compute weighted-avg fill_price from broker.get_trades()
Phase 2 (Event-gated): Compare DB fill_price vs broker.get_positions().average_price
Phase 3 (On exit): Recompute realized P&L from exit fills + charges

Phase 1 and 3 run synchronously; Phase 2 is event-gated (caller triggers on
every order_state transition, plus a 60s slow sweep).

All phases return PriceTruthResult carrying the finding and corrective action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.brokers.base import BrokerInterface

from src.brokers.base import Fill, Holding, Position


class PriceTruthStatus(StrEnum):
    OK = "OK"
    CORRECTED = "CORRECTED"
    MISMATCH = "MISMATCH"
    CRITICAL = "CRITICAL"  # quantity mismatch — requires human intervention
    NO_DATA = "NO_DATA"  # no fills or no position found


@dataclass
class PriceTruthResult:
    phase: int
    status: PriceTruthStatus
    symbol: str
    broker_order_id: str
    expected_price: float = 0.0
    actual_price: float = 0.0
    corrected_price: float = 0.0
    message: str = ""
    fills: list[Fill] = field(default_factory=list)


class PriceTruthEngine:
    """
    Verifies fill prices through 3 independent phases.

    Instantiate once per OrderLifecycleManager; call each phase
    at the appropriate point in the order lifecycle.
    """

    MISMATCH_TOLERANCE = 0.01  # 1 paisa tolerance for float comparison

    def __init__(self, broker: BrokerInterface) -> None:
        self._broker = broker

    async def phase1_on_fill(
        self,
        broker_order_id: str,
        symbol: str,
        recorded_fill_price: float,
    ) -> PriceTruthResult:
        """
        Phase 1: Re-fetch fills from broker, compute weighted-avg fill price.

        Compares against `recorded_fill_price` (from earlier lifecycle poll).
        Returns CORRECTED if a discrepancy is found.
        """
        fills = await self._broker.get_trades(broker_order_id)

        if not fills:
            return PriceTruthResult(
                phase=1,
                status=PriceTruthStatus.NO_DATA,
                symbol=symbol,
                broker_order_id=broker_order_id,
                message="No fills returned from broker",
            )

        total_qty = sum(f.quantity for f in fills)
        if total_qty == 0:
            return PriceTruthResult(
                phase=1,
                status=PriceTruthStatus.NO_DATA,
                symbol=symbol,
                broker_order_id=broker_order_id,
                message="Fills have zero total quantity",
            )

        weighted_price = sum(f.quantity * f.price for f in fills) / total_qty

        if abs(weighted_price - recorded_fill_price) > self.MISMATCH_TOLERANCE:
            return PriceTruthResult(
                phase=1,
                status=PriceTruthStatus.CORRECTED,
                symbol=symbol,
                broker_order_id=broker_order_id,
                expected_price=recorded_fill_price,
                actual_price=weighted_price,
                corrected_price=weighted_price,
                message=f"fill_price corrected {recorded_fill_price:.2f} → {weighted_price:.2f}",
                fills=fills,
            )

        return PriceTruthResult(
            phase=1,
            status=PriceTruthStatus.OK,
            symbol=symbol,
            broker_order_id=broker_order_id,
            expected_price=recorded_fill_price,
            actual_price=weighted_price,
            fills=fills,
        )

    async def phase2_event_gated(
        self,
        symbol: str,
        broker_order_id: str,
        recorded_fill_price: float,
        recorded_quantity: int,
        product: str = "MIS",
    ) -> PriceTruthResult:
        """
        Phase 2: Compare recorded fill_price against broker truth.

        MIS/intraday has no separate holdings concept, so get_positions() (today's
        position book) is the right source. CNC/delivery is different: once ANY
        same-day transaction touches that symbol (e.g. a same-day sell against an
        existing holding), Kite's positions() reports THAT transaction's price —
        not the holding's cost basis — and normalize_position() takes abs(quantity),
        so a same-day sell of the full holding still "matches" recorded_quantity
        and silently overwrites a correct fill_price with the sell price (found
        2026-09-02: IFCI's real entry 84.27 was getting stomped to today's exit
        price 98.22 every ~30s). CNC must compare against get_holdings() instead,
        which reports the actual holding cost basis.

        Quantity mismatch → CRITICAL (requires immediate human intervention).
        Price mismatch within tolerance → CORRECTED with broker's value.
        """
        pos: Holding | Position | None
        if product.upper() == "CNC":
            holdings = await self._broker.get_holdings()
            pos = next((h for h in holdings if h.symbol == symbol), None)
        else:
            positions = await self._broker.get_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)

        if pos is None:
            return PriceTruthResult(
                phase=2,
                status=PriceTruthStatus.NO_DATA,
                symbol=symbol,
                broker_order_id=broker_order_id,
                message=f"No open position found for {symbol!r}",
            )

        # A Holding bought the previous trading day can sit with quantity=0 /
        # t1_quantity=N until Zerodha finishes settling it into `quantity` — real,
        # currently-held shares, not a lesser state (Position has no such split,
        # so getattr degrades to 0 there and this is a no-op for MIS/intraday).
        # Found 2026-09-08: CGCL, genuinely held (confirmed directly against the
        # broker, GTT still active), false-alarmed CRITICAL here every cycle
        # because this only ever read `quantity` alone — the same class of bug
        # already fixed in src/supervisor/reconciler.py's _effective_qty.
        effective_qty = pos.quantity + getattr(pos, "t1_quantity", 0)
        if effective_qty != recorded_quantity:
            return PriceTruthResult(
                phase=2,
                status=PriceTruthStatus.CRITICAL,
                symbol=symbol,
                broker_order_id=broker_order_id,
                expected_price=recorded_fill_price,
                actual_price=pos.average_price,
                message=(
                    f"CRITICAL quantity mismatch for {symbol!r}: "
                    f"DB={recorded_quantity} broker={effective_qty}"
                ),
            )

        if abs(pos.average_price - recorded_fill_price) > self.MISMATCH_TOLERANCE:
            return PriceTruthResult(
                phase=2,
                status=PriceTruthStatus.CORRECTED,
                symbol=symbol,
                broker_order_id=broker_order_id,
                expected_price=recorded_fill_price,
                actual_price=pos.average_price,
                corrected_price=pos.average_price,
                message=(
                    f"fill_price auto-corrected from {recorded_fill_price:.2f} "
                    f"to {pos.average_price:.2f} (broker position)"
                ),
            )

        return PriceTruthResult(
            phase=2,
            status=PriceTruthStatus.OK,
            symbol=symbol,
            broker_order_id=broker_order_id,
            expected_price=recorded_fill_price,
            actual_price=pos.average_price,
        )

    async def phase3_on_exit(
        self,
        exit_order_id: str,
        symbol: str,
        entry_fill_price: float,
        quantity: int,
        direction: str,
    ) -> PriceTruthResult:
        """
        Phase 3: Re-fetch exit fills, compute realized P&L from actual prices.

        Returns PriceTruthResult with corrected_price = actual exit price.
        P&L = (exit_price - entry_price) * qty for LONG,
              (entry_price - exit_price) * qty for SHORT.
        """
        fills = await self._broker.get_trades(exit_order_id)

        if not fills:
            return PriceTruthResult(
                phase=3,
                status=PriceTruthStatus.NO_DATA,
                symbol=symbol,
                broker_order_id=exit_order_id,
                message="No exit fills returned from broker",
            )

        total_qty = sum(f.quantity for f in fills)
        if total_qty == 0:
            return PriceTruthResult(
                phase=3,
                status=PriceTruthStatus.NO_DATA,
                symbol=symbol,
                broker_order_id=exit_order_id,
                message="Exit fills have zero total quantity",
            )

        exit_price = sum(f.quantity * f.price for f in fills) / total_qty

        if direction.upper() == "BUY":
            pnl = (exit_price - entry_fill_price) * quantity
        else:
            pnl = (entry_fill_price - exit_price) * quantity

        return PriceTruthResult(
            phase=3,
            status=PriceTruthStatus.OK,
            symbol=symbol,
            broker_order_id=exit_order_id,
            expected_price=entry_fill_price,
            actual_price=exit_price,
            corrected_price=exit_price,
            message=f"Realized P&L: {pnl:.2f}",
            fills=fills,
        )
