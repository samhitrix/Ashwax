"""Pre-trade setup fingerprinter — per-signal setup snapshot before entry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SetupFingerprint:
    signal_id: str
    symbol: str
    strategy_name: str
    regime: str
    vix: float
    rs_rank: float
    setup_quality_score: float
    volume_ratio: float
    atr_pct: float
    days_since_breakout: int
    context: dict[str, Any] = field(default_factory=dict)


class PreTradeFingerprinter:
    """Captures market setup fingerprint at signal time."""

    def fingerprint(
        self, signal_id: str, symbol: str, strategy_name: str, context: dict[str, Any]
    ) -> SetupFingerprint:
        return SetupFingerprint(
            signal_id=signal_id,
            symbol=symbol,
            strategy_name=strategy_name,
            regime=context.get("regime", "UNKNOWN"),
            vix=float(context.get("vix", 0)),
            rs_rank=float(context.get("rs_rank", 0)),
            setup_quality_score=float(context.get("quality_score", 0)),
            volume_ratio=float(context.get("volume_ratio", 1.0)),
            atr_pct=float(context.get("atr_pct", 0)),
            days_since_breakout=int(context.get("days_since_breakout", 0)),
            context=context,
        )
