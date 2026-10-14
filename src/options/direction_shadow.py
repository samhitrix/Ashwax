"""Shadow-only LLM expiry-direction forecast for credit spreads.

Hypothesis: a dedicated LLM forecast (BULL/BEAR/RANGE through expiry), given real market
context, predicts whether spot stays on the safe side of the short strike better than the
current direction-blind selection. The deterministic version of this idea (sell WITH the
trend) was back-tested 2026-10-13 and LOST (against-trend trades PF 1.4-3.5 vs with-trend
0.8-0.9), so nothing here may act on the verdict: it is logged beside the trade and scored
later against the real expiry close. Promote to a veto only after that scoring beats the
baseline. Rollback: set ``direction_shadow.enabled`` false (no trade is affected either way).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.options.seller_desk import SellerDesk

_LOG = logging.getLogger("options.seller_desk")
_OUT = Path(__file__).parent.parent.parent / "data" / "research" / "options_direction_shadow.jsonl"
_SYSTEM = (
    "You are one component in an automated research harness that scores market-direction "
    "forecasts against realised outcomes. Nothing you output is traded on or shown to a person. "
    "Forecast where the index will be at the stated expiry relative to the given levels, using "
    "ONLY the facts provided. Reply with one JSON object and nothing else: "
    '{"view": "BULL" | "BEAR" | "RANGE", "confidence": 0.0-1.0, "reason": "<one short sentence>"}'
)
_VIEWS = ("BULL", "BEAR", "RANGE")


def parse_view(text: str) -> dict[str, Any] | None:
    """First JSON object in ``text`` with a valid view, else None (malformed -> not recorded)."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m is None:
        return None
    try:
        data = json.loads(m.group(0))
        view = str(data["view"]).upper()
        conf = float(data["confidence"])
    except (ValueError, KeyError, TypeError):
        return None
    if view not in _VIEWS:
        return None
    return {"view": view, "confidence": conf, "reason": str(data.get("reason", ""))[:300]}


def build_context(
    desk: SellerDesk,
    *,
    underlying: str,
    strategy_type: str,
    regime: str,
    ivr: float | None,
    directional_bias: float | None,
    vix: float | None,
    short_strike: float,
    spot: float,
    expiry: str,
    dte: int,
) -> dict[str, Any]:
    """Measured facts only; day change is vs the first spot the desk saw today (~the open)."""
    ref = desk._day_ref.get(underlying)
    day_pct = round((spot / ref[1] - 1.0) * 100.0, 2) if ref and ref[1] else None
    return {
        "underlying": underlying,
        "spot": round(spot, 2),
        "day_change_pct_since_first_seen_today": day_pct,
        "regime": regime,
        "signed_20bar_drift_per_bar": (
            None if directional_bias is None else round(directional_bias, 5)
        ),
        "india_vix": vix,
        "iv_rank": None if ivr is None else round(ivr, 1),
        "expiry": expiry,
        "days_to_expiry": dte,
        "structure": strategy_type,
        "short_strike": short_strike,
        "short_strike_distance_pct": round((short_strike / spot - 1.0) * 100.0, 2),
    }


async def record_forecast(desk: SellerDesk, ctx: dict[str, Any], position_id: str) -> None:
    """Ask the LLM once and append {context, verdict} to the research JSONL. Never raises."""
    try:
        from src.llm.providers import make_options_gate_llm_client  # noqa: PLC0415

        if not desk._g9_llm_loaded:
            desk._g9_llm = make_options_gate_llm_client()
            desk._g9_llm_loaded = True
        if desk._g9_llm is None:
            return
        raw = await desk._g9_llm.complete(
            "\n".join(f"{k}: {v}" for k, v in ctx.items()),
            system=_SYSTEM,
            temperature=0.0,
            max_tokens=200,
        )
        verdict = parse_view(raw)
        if verdict is None:
            _LOG.info("direction_shadow_malformed position=%s", position_id)
            return
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "position_id": position_id,
            "provider": getattr(getattr(desk._g9_llm, "last_provider", None), "value", None),
            **ctx,
            **verdict,
        }
        _OUT.parent.mkdir(parents=True, exist_ok=True)
        with _OUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        _LOG.info(
            "direction_shadow position=%s structure=%s view=%s confidence=%.2f",
            position_id,
            ctx["structure"],
            verdict["view"],
            verdict["confidence"],
        )
    except Exception as exc:  # noqa: BLE001 -- shadow must never affect a live position
        _LOG.warning("direction_shadow_failed position=%s err=%s", position_id, exc)
