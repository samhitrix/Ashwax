"""Minimum-quantity floor with a conviction-scaled range (swing + intraday equity).

Opt-in per segment and per mode via config/capital.json ``<segment>.min_qty_floor``.
When enabled, quantity is no longer the bare risk-cap quantity:

    max_qty = min(ceiling_rupees / stop distance, affordable shares[, liquidity cap])
    qty     = min_qty + conviction x (max_qty - min_qty)      when max_qty >= min_qty
    qty     = max_qty                                         otherwise (reduce, never skip)

``conviction`` (0..1) is a product of existing soft signals -- daily 50/200-DMA trend,
setup_quality_score and LLM confidence bands. It is NOT a calibrated probability; the
value is persisted per trade so its predictive power can be measured before any live use.
The floor never refuses a trade; every other CRM gate (G1/G7/G9, daily-loss breaker)
still applies unchanged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.risk.budget import parse_pct

_log = logging.getLogger(__name__)
_PATH = Path(__file__).resolve().parents[2] / "config" / "capital.json"


@dataclass(frozen=True)
class QtyFloorConfig:
    """Parsed ``<segment>.min_qty_floor`` block."""

    min_qty: int
    max_loss_per_trade_pct: float
    counter_trend_mult: float = 0.5
    setup_below_40: float = 0.5
    setup_40_to_70: float = 0.75
    setup_70_plus: float = 1.0
    llm_below_0_6: float = 0.75
    llm_0_6_plus: float = 1.0


def load_qty_floor(segment: str, is_paper: bool, path: Path | None = None) -> QtyFloorConfig | None:
    """Return the floor config when enabled for this segment+mode, else None.

    Missing block / switch off => None, so callers keep today's sizing exactly.
    A malformed block also returns None (logged): sizing falls back to the risk-capped
    quantity, which is the safer of the two.
    """
    try:
        raw = json.loads((path or _PATH).read_text(encoding="utf-8"))
        blk: dict[str, Any] = (raw.get(segment) or {}).get("min_qty_floor") or {}
        if not blk.get("enabled_paper" if is_paper else "enabled_live", False):
            return None
        conv = blk.get("conviction") or {}
        sq = conv.get("setup_quality") or {}
        lc = conv.get("llm_confidence") or {}
        return QtyFloorConfig(
            min_qty=int(blk["min_qty"]),
            max_loss_per_trade_pct=parse_pct(
                blk["max_loss_per_trade"], f"{segment}.min_qty_floor.max_loss_per_trade"
            ),
            counter_trend_mult=float(conv.get("counter_trend_mult", 0.5)),
            setup_below_40=float(sq.get("below_40", 0.5)),
            setup_40_to_70=float(sq.get("40_to_70", 0.75)),
            setup_70_plus=float(sq.get("70_plus", 1.0)),
            llm_below_0_6=float(lc.get("below_0.6", 0.75)),
            llm_0_6_plus=float(lc.get("0.6_plus", 1.0)),
        )
    except Exception as exc:  # bad config must not break entry evaluation
        _log.warning("qty_floor_config_invalid segment=%s err=%s", segment, exc)
        return None


def conviction_score(
    dma_mult: float | None,
    setup_q: float | None,
    llm_conf: float | None,
    cfg: QtyFloorConfig,
) -> float:
    """Product of the configured band multipliers, clamped to [0, 1].

    Each missing input is neutral (1.0) -- absence of evidence never shrinks size.
    """
    c = 1.0
    if dma_mult is not None and dma_mult < 1.0:
        c *= cfg.counter_trend_mult
    if setup_q is not None:
        if setup_q < 40:
            c *= cfg.setup_below_40
        elif setup_q < 70:
            c *= cfg.setup_40_to_70
        else:
            c *= cfg.setup_70_plus
    if llm_conf is not None:
        c *= cfg.llm_below_0_6 if llm_conf < 0.6 else cfg.llm_0_6_plus
    return max(0.0, min(1.0, c))


def floored_quantity(
    entry: float,
    sl: float,
    min_qty: int,
    ceiling_rupees: float,
    available_funds: float,
    conviction: float,
    extra_cap_qty: int | None = None,
) -> tuple[int, str]:
    """Quantity between ``min_qty`` and the risk/cash ceiling, scaled by conviction.

    Returns (qty, reason). reason is "scaled", "reduced_ceiling", "reduced_cash" or
    "reduced_liquidity" when max_qty < min_qty. qty is 0 only when not even one share
    fits the ceiling/cash -- the same no-funds outcome as today's sizer.
    """
    risk_per_share = abs(entry - sl)
    if entry <= 0 or risk_per_share <= 0:
        return 0, "invalid"
    caps = {
        "ceiling": int(max(0.0, ceiling_rupees) / risk_per_share),
        "cash": int(max(0.0, available_funds) / entry),
    }
    if extra_cap_qty is not None:
        caps["liquidity"] = max(0, int(extra_cap_qty))
    binding = min(caps, key=lambda k: caps[k])
    max_qty = caps[binding]
    if max_qty <= 0:
        return 0, f"reduced_{binding}"
    if max_qty < min_qty:
        return max_qty, f"reduced_{binding}"
    c = max(0.0, min(1.0, conviction))
    return min_qty + int(c * (max_qty - min_qty)), "scaled"


async def daily_trend_inputs(
    ohlcv_repo: Any, sym: str, entry: float, direction: str
) -> tuple[float | None, float | None]:
    """(dma_mult, setup_quality_score) from 220 daily bars -- same math intraday uses.

    Best-effort: any missing data or error returns (None, None) = neutral conviction.
    """
    if ohlcv_repo is None:
        return None, None
    try:
        from src.risk.trend_filter import daily_trend_size_multiplier
        from src.strategies.indicators.library import quality_score

        df = await ohlcv_repo.fetch_dataframe(sym, "1d", 220)
        if df is None or len(df) < 200:
            return None, None
        s50 = float(df["close"].rolling(50).mean().iloc[-1])
        s200 = float(df["close"].rolling(200).mean().iloc[-1])
        dma = daily_trend_size_multiplier(float(entry), s50, s200, direction)
        try:
            sq: float | None = round(float(quality_score(df).iloc[-1]), 1)
        except Exception:
            sq = None
        return dma, sq
    except Exception as exc:
        _log.debug("qty_floor_trend_inputs_skipped sym=%s err=%s", sym, exc)
        return None, None


def apply_qty_floor(
    crm_signal: Any,
    *,
    segment: str,
    is_paper: bool,
    entry: float,
    sl: float,
    equity: float,
    available_funds: float,
    dma_mult: float | None,
    setup_q: float | None,
    extra_cap_qty: int | None = None,
    log: logging.Logger | logging.LoggerAdapter[Any] | None = None,
) -> bool:
    """Re-size ``crm_signal`` in place when the floor is enabled. Returns True if applied.

    Called right before crm.evaluate so the LLM verdict (if any) is already in meta.
    Sets meta qty_floor_cap_rupees so G8 sizes against the same ceiling, and
    max_risk_rupees so the post-fill cap matches.
    """
    cfg = load_qty_floor(segment, is_paper)
    if cfg is None or equity <= 0:
        return False
    meta = crm_signal.meta
    llm_conf = float(meta["llm_confidence"]) if meta.get("llm_evaluated") else None
    conv = conviction_score(dma_mult, setup_q, llm_conf, cfg)
    ceiling = equity * cfg.max_loss_per_trade_pct / 100.0
    qty, reason = floored_quantity(
        entry, sl, cfg.min_qty, ceiling, available_funds, conv, extra_cap_qty
    )
    (log or _log).info(
        "qty_floor seg=%s sym=%s mode=%s min=%d ceiling_rupees=%.0f conviction=%.2f dma=%s "
        "setup_q=%s llm=%s old_qty=%d qty=%d reason=%s",
        segment,
        crm_signal.symbol,
        "paper" if is_paper else "live",
        cfg.min_qty,
        ceiling,
        conv,
        dma_mult,
        setup_q,
        llm_conf,
        crm_signal.intended_quantity,
        qty,
        reason,
    )
    if qty <= 0:
        return False  # nothing fits: keep today's sizing outcome untouched
    crm_signal.intended_quantity = qty
    crm_signal.risk_rupees = abs(entry - sl) * qty
    meta["qty_floor_cap_rupees"] = ceiling
    meta["qty_floor_min_qty"] = cfg.min_qty
    meta["qty_floor_conviction"] = round(conv, 3)
    meta["qty_floor_reason"] = reason
    meta["max_risk_rupees"] = ceiling
    return True
