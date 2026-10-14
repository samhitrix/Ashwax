"""
PaperGTTSimulator — simulates GTT (SL+target) triggers using real LTP.

Paper GTTs are NOT broker orders — tracked here.
Checked every 2-3s against real LTP from broker.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PaperGTT:
    agent_trade_id: str
    symbol: str
    sl_trigger: float
    target_trigger: float
    quantity: int
    direction: str  # BUY | SELL (direction of original entry trade)


@dataclass
class GTTTriggerResult:
    agent_trade_id: str
    triggered: bool
    trigger_type: str  # SL_HIT | TARGET_HIT
    fill_price: float


class PaperGTTSimulator:
    """Simulates GTT orders for paper positions against real LTP."""

    def __init__(self) -> None:
        self._gtts: dict[str, PaperGTT] = {}

    def arm(self, gtt: PaperGTT) -> None:
        self._gtts[gtt.agent_trade_id] = gtt

    def disarm(self, agent_trade_id: str) -> None:
        self._gtts.pop(agent_trade_id, None)

    def update(
        self,
        agent_trade_id: str,
        sl: float | None = None,
        target: float | None = None,
        quantity: int | None = None,
    ) -> None:
        """Re-arm an existing GTT — used by the smart-exit pass to ratchet the stop
        (breakeven / ATR trail) and shrink quantity after a partial book."""
        gtt = self._gtts.get(agent_trade_id)
        if gtt is None:
            return
        if sl is not None:
            gtt.sl_trigger = sl
        if target is not None:
            gtt.target_trigger = target
        if quantity is not None:
            gtt.quantity = quantity

    def check(self, symbol: str, ltp: float) -> list[GTTTriggerResult]:
        """Return trigger results for all GTTs on this symbol at this LTP.
        Handles BOTH directions — a SELL (short) entry stops out ABOVE and targets
        BELOW. (The long-only version silently never exited paper shorts.)"""
        results: list[GTTTriggerResult] = []
        for tid, gtt in list(self._gtts.items()):
            if gtt.symbol != symbol:
                continue
            is_long = gtt.direction.upper() == "BUY"
            if is_long:
                if ltp <= gtt.sl_trigger:
                    results.append(GTTTriggerResult(tid, True, "SL_HIT", ltp))
                elif gtt.target_trigger > 0 and ltp >= gtt.target_trigger:
                    results.append(GTTTriggerResult(tid, True, "TARGET_HIT", ltp))
            else:
                if ltp >= gtt.sl_trigger:
                    results.append(GTTTriggerResult(tid, True, "SL_HIT", ltp))
                elif gtt.target_trigger > 0 and ltp <= gtt.target_trigger:
                    results.append(GTTTriggerResult(tid, True, "TARGET_HIT", ltp))
        return results

    def get_all(self) -> list[PaperGTT]:
        return list(self._gtts.values())
