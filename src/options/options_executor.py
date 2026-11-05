"""Options execution via BrokerInterface (Phase 6, Layer 5).

Places a chosen multi-leg :class:`StructureChoice` on the NFO segment, honouring
the mission invariants:

* **SL always** — a premium-based stop is placed on the primary long leg for
  net-debit directional structures; ``protective_put`` is self-hedged by its put.
* **Defined risk only** — the upstream selector + :class:`OptionsRiskGate`
  guarantee net-debit / covered structures before we ever get here.
* **Atomic** — legs are placed long-first; if any leg fails, already-filled legs
  are squared off (market) and the error is surfaced. No silent partial book.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.brokers.base import OrderRequest
from src.options.structure_selector import OptionLeg, StructureChoice

if TYPE_CHECKING:
    from src.brokers.base import BrokerInterface

_LOG = logging.getLogger("options.options_executor")


@dataclass
class StructureResult:
    structure_id: str
    structure_type: str
    leg_order_ids: list[str] = field(default_factory=list)
    sl_order_id: str | None = None
    equity_order_id: str | None = None  # protective_put underlying leg
    equity_symbol: str = ""  # "NSE:RELIANCE" — kept for unwind
    equity_qty: int = 0


class OptionsExecutionError(RuntimeError):
    """Raised when a leg fails and the structure is unwound."""


def _split_symbol(tradingsymbol: str) -> tuple[str, str]:
    """'NFO:RELIANCE24JUL2750CE' -> ('NFO', 'RELIANCE24JUL2750CE')."""
    if ":" in tradingsymbol:
        exch, sym = tradingsymbol.split(":", 1)
        return exch, sym
    return "NFO", tradingsymbol


class OptionsExecutor:
    def __init__(
        self, broker: BrokerInterface, options_repo: Any = None, is_paper: bool = True
    ) -> None:
        self._broker = broker
        self._repo = options_repo
        self._is_paper = is_paper

    async def place_structure(self, choice: StructureChoice, signal_id: str) -> StructureResult:
        if not choice.legs:
            raise OptionsExecutionError("structure has no option legs")

        structure_id = f"OPT-{uuid.uuid4().hex[:12].upper()}"
        _LOG.debug(
            "place_structure_received id=%s signal=%s type=%s legs=%d stop=%s",
            structure_id,
            signal_id,
            choice.structure_type,
            len(choice.legs),
            choice.stop,
        )
        result = StructureResult(structure_id=structure_id, structure_type=choice.structure_type)

        # protective_put carries an equity leg (long underlying) not in choice.legs.
        if choice.structure_type == "protective_put":
            result.equity_order_id = await self._place_equity_leg(choice, signal_id, result)

        # Long legs first so a short leg never sits naked if a later leg fails.
        legs = sorted(choice.legs, key=lambda leg_: 0 if leg_.direction == "BUY" else 1)
        filled: list[OptionLeg] = []
        try:
            for leg in legs:
                order_id = await self._place_leg(leg, signal_id, "MARKET")
                result.leg_order_ids.append(order_id)
                filled.append(leg)
        except Exception as exc:  # noqa: BLE001 — unwind then re-raise
            await self._unwind(filled, result, signal_id)
            raise OptionsExecutionError(f"leg placement failed: {exc}") from exc

        # SL on the primary long leg (premium-based). protective_put is hedged.
        if choice.structure_type != "protective_put" and choice.stop > 0:
            try:
                result.sl_order_id = await self._place_stop(choice, signal_id)
            except Exception as exc:  # noqa: BLE001 — SL must not be silently skipped
                await self._unwind(filled, result, signal_id)
                raise OptionsExecutionError(f"SL placement failed: {exc}") from exc

        if self._repo is not None:
            try:
                await self._repo.insert_structure(
                    structure_id, signal_id, choice, result, is_paper=self._is_paper
                )
            except Exception as exc:  # noqa: BLE001 — persistence best-effort, log is truth
                _LOG.warning("options_persist_failed id=%s err=%s", structure_id, exc)

        _LOG.info(
            "options_structure_placed id=%s type=%s legs=%d sl=%s",
            structure_id,
            choice.structure_type,
            len(result.leg_order_ids),
            result.sl_order_id,
        )
        return result

    async def close_structure_at_market(
        self,
        legs: list[dict[str, Any]],
        sl_order_id: str | None,
        signal_id: str,
    ) -> float | None:
        """Market-close every persisted option leg of a structure whose target
        has been reached (structure_monitor.py, Options learning-engine PR5).

        Cancels the still-resting SL first (best-effort — a stale/already-
        filled SL is not fatal here, this is the normal "we exited via target
        instead of the stop" case). Returns the realized_pnl computed from
        each leg's LTP captured immediately before its close order — the same
        documented APPROXIMATION pattern already used by this codebase's
        seller-desk close path (options_leg_executor.py's close_leg), not a
        confirmed fill price. `legs` is the raw options_structures.legs JSON
        (dicts, not OptionLeg objects — this reconstructs from persisted DB
        state, not from the in-memory StructureChoice, since the sweep that
        calls this runs long after the original in-memory objects are gone).
        """
        if sl_order_id:
            try:
                await self._broker.cancel_order(sl_order_id)
            except Exception as exc:  # noqa: BLE001 — best-effort, may already be filled/gone
                _LOG.debug("close_structure_sl_cancel_failed order_id=%s err=%s", sl_order_id, exc)

        total_pnl = 0.0
        any_price_known = False
        for leg in legs:
            tradingsymbol = str(leg.get("tradingsymbol", ""))
            if not tradingsymbol:
                continue
            entry_premium = float(leg.get("premium", 0.0))
            lots = int(leg.get("lots", 0))
            lot_size = int(leg.get("lot_size", 0))
            quantity = lots * lot_size
            direction = str(leg.get("direction", "BUY"))
            exit_direction = "SELL" if direction == "BUY" else "BUY"
            exch, sym = _split_symbol(tradingsymbol)
            exit_price: float | None = None
            try:
                quotes = await self._broker.get_ltp([tradingsymbol])
                exit_price = quotes.get(tradingsymbol)
            except Exception as exc:  # noqa: BLE001 — LTP is for P&L bookkeeping, never blocks the close
                _LOG.warning(
                    "close_structure_ltp_fetch_failed symbol=%s err=%s", tradingsymbol, exc
                )
            await self._broker.place_order(
                OrderRequest(
                    symbol=sym,
                    exchange=exch,
                    direction=exit_direction,
                    quantity=quantity,
                    order_type="MARKET",
                    product="MARGIN",
                    validity="DAY",
                    agent_trade_id=f"{signal_id}-CLOSE-{int(float(leg.get('strike', 0)))}"[:32],
                    signal_id=signal_id,
                    tag="opt-target-close",
                )
            )
            if exit_price is not None:
                any_price_known = True
                sign = 1.0 if direction == "BUY" else -1.0
                total_pnl += sign * (exit_price - entry_premium) * quantity

        return total_pnl if any_price_known else None

    # ── leg helpers ──────────────────────────────────────────────────────────

    async def _place_leg(self, leg: OptionLeg, signal_id: str, order_type: str) -> str:
        exch, sym = _split_symbol(leg.tradingsymbol)
        req = OrderRequest(
            symbol=sym,
            exchange=exch,
            direction=leg.direction,
            quantity=leg.quantity,
            order_type=order_type,
            product="MARGIN",
            validity="DAY",
            agent_trade_id=f"{signal_id}-{leg.instrument_type}-{int(leg.strike)}"[:32],
            signal_id=signal_id,
            tag="opt",
            price=leg.premium if order_type == "LIMIT" else None,
        )
        order_id = await self._broker.place_order(req)
        _LOG.debug(
            "options_leg_placed signal=%s symbol=%s direction=%s strike=%s type=%s "
            "qty=%d order_id=%s",
            signal_id,
            leg.tradingsymbol,
            leg.direction,
            leg.strike,
            leg.instrument_type,
            leg.quantity,
            order_id,
        )
        return order_id

    async def _place_equity_leg(
        self, choice: StructureChoice, signal_id: str, result: StructureResult
    ) -> str:
        exch, sym = _split_symbol(choice.symbol)
        lot = choice.legs[0].lot_size
        result.equity_symbol = choice.symbol
        result.equity_qty = lot
        req = OrderRequest(
            symbol=sym,
            exchange=exch or "NSE",
            direction="BUY",
            quantity=lot,
            order_type="MARKET",
            product="DELIVERY",
            validity="DAY",
            agent_trade_id=f"{signal_id}-EQ"[:32],
            signal_id=signal_id,
            tag="opt-pp",
        )
        return await self._broker.place_order(req)

    async def _place_stop(self, choice: StructureChoice, signal_id: str) -> str:
        """Premium-based SL-M on the primary long leg (lowest-strike BUY)."""
        long_legs = [leg for leg in choice.legs if leg.direction == "BUY"]
        primary = min(long_legs, key=lambda leg_: leg_.strike)
        exch, sym = _split_symbol(primary.tradingsymbol)
        req = OrderRequest(
            symbol=sym,
            exchange=exch,
            direction="SELL",  # exit the long leg
            quantity=primary.quantity,
            order_type="SL-M",
            product="MARGIN",
            validity="DAY",
            agent_trade_id=f"{signal_id}-SL"[:32],
            signal_id=signal_id,
            tag="opt-sl",
            trigger_price=primary.premium * 0.5,  # 50% premium stop
        )
        return await self._broker.place_order(req)

    async def _unwind(
        self, filled: list[OptionLeg], result: StructureResult, signal_id: str
    ) -> None:
        """Square off already-filled legs (and any equity leg) — no silent partial."""
        for leg in filled:
            exch, sym = _split_symbol(leg.tradingsymbol)
            exit_dir = "SELL" if leg.direction == "BUY" else "BUY"
            try:
                await self._broker.place_order(
                    OrderRequest(
                        symbol=sym,
                        exchange=exch,
                        direction=exit_dir,
                        quantity=leg.quantity,
                        order_type="MARKET",
                        product="MARGIN",
                        validity="DAY",
                        agent_trade_id=f"{signal_id}-UNWIND-{int(leg.strike)}"[:32],
                        signal_id=signal_id,
                        tag="opt-unwind",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.critical("options_unwind_failed leg=%s err=%s", leg.tradingsymbol, exc)
        if result.equity_order_id is not None and result.equity_qty > 0:
            exch, sym = _split_symbol(result.equity_symbol)
            try:
                await self._broker.place_order(
                    OrderRequest(
                        symbol=sym,
                        exchange=exch or "NSE",
                        direction="SELL",
                        quantity=result.equity_qty,
                        order_type="MARKET",
                        product="DELIVERY",
                        validity="DAY",
                        agent_trade_id=f"{signal_id}-UNWIND-EQ"[:32],
                        signal_id=signal_id,
                        tag="opt-unwind",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.critical("options_equity_unwind_failed err=%s", exc)
        _LOG.warning("options_structure_unwound id=%s legs=%d", result.structure_id, len(filled))
