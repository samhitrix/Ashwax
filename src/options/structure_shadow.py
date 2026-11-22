"""Shadow-only forward record of iron condor / iron butterfly / calendar vs spread controls.

Hypothesis (docs/hypotheses/R_condor_fly_calendar_forward_shadow.md): built from the REAL live
chain with realistic fills (sell at bid, buy at ask), these structures earn positive return on
max loss and beat the same-day one-sided credit spread. They never trade today because the
desk gate (range-bound regime + IVR floor) is almost unreachable, so there is no data. This
module records one snapshot per (IST day, underlying, structure) regardless of regime and
settles offline (scripts/research/settle_structure_shadow.py).

It never opens a position and never touches candidates, CRM, margin, the executor or the DB.
Every failure is swallowed. Rollback: set ``structure_shadow.enabled`` false.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.options import seller_settings as _seller_settings
from src.options.expiry_selector import dte as compute_dte
from src.options.expiry_selector import dte_band, select_calendar_expiries
from src.options.structure_builder import build_structure, build_two_expiry_structure

if TYPE_CHECKING:
    from src.options.nfo_chain import OptionChain
    from src.options.seller_desk import SellerDesk
    from src.options.structure_selector import OptionLeg

_LOG = logging.getLogger("options.seller_desk")
_OUT = Path(__file__).parent.parent.parent / "data" / "research" / "options_structure_shadow.jsonl"

SINGLE_EXPIRY_TYPES = ("iron_condor", "iron_butterfly", "bull_put_spread", "bear_call_spread")
CALENDAR_TYPE = "calendar_spread"
DEFAULT_TYPES = (*SINGLE_EXPIRY_TYPES, CALENDAR_TYPE)

# (IST date iso, underlying, structure) already recorded; one row per key per day. Seeded from the
# output file (a launcher restart must not write a second row for the same day).
_DONE: set[tuple[str, str, str]] = set()
_SEEDED_FROM: Path | None = None
# Keys whose skip reason was already logged today (retried every cycle, logged once).
_SKIP_LOGGED: set[tuple[str, str, str]] = set()


def _seed_done_from_file() -> None:
    global _SEEDED_FROM  # noqa: PLW0603
    if _SEEDED_FROM == _OUT:
        return
    _SEEDED_FROM = _OUT
    if not _OUT.exists():
        return
    for line in _OUT.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            _DONE.add((r["date"], r["underlying"], r["structure"]))
        except (ValueError, KeyError, TypeError):
            continue  # a malformed line must not stop the shadow


def why_no_structure(chain: OptionChain, structure: str, ivr: float | None) -> str:
    """Why the desk's builder found no legs: per target delta, how many strikes were inside the
    delta tolerance vs how many also passed the desk's liquidity test (OI / volume / spread)."""
    from src.options import structure_builder as sb  # noqa: PLC0415
    from src.options.strike_selector import _DELTA_TOLERANCE, row_is_liquid  # noqa: PLC0415

    sd, ld = sb.short_delta(ivr), sb._long_delta(ivr)  # noqa: SLF001
    targets = {
        "bull_put_spread": [("PE", sd), ("PE", ld)],
        "bear_call_spread": [("CE", sd), ("CE", ld)],
        "iron_condor": [("PE", sd), ("PE", ld), ("CE", sd), ("CE", ld)],
        "iron_butterfly": [("CE", ld), ("PE", ld)],
    }.get(structure, [])
    parts = []
    for kind, delta in targets:
        signed = delta if kind == "CE" else -delta
        cands = [
            r
            for r in chain.strikes
            if (d := (r.call_delta if kind == "CE" else r.put_delta)) is not None
            and abs(d - signed) <= _DELTA_TOLERANCE
        ]
        liquid = sum(row_is_liquid(r, kind) for r in cands)
        parts.append(f"{kind}@{delta:.2f} in_tolerance={len(cands)} liquid={liquid}")
    return "; ".join(parts) or "builder_returned_none"


def _quote_and_iv(chain: OptionChain, strike: float, kind: str) -> tuple[Any, float | None]:
    for row in chain.strikes:
        if row.strike == strike:
            return (row.call, row.call_iv) if kind == "CE" else (row.put, row.put_iv)
    return None, None


def leg_rows(legs: list[OptionLeg], chain: OptionChain) -> list[dict[str, Any]]:
    """Per-leg snapshot with the real bid/ask looked up from the chain the leg was built from."""
    out: list[dict[str, Any]] = []
    for leg in legs:
        quote, iv = _quote_and_iv(chain, leg.strike, leg.instrument_type)
        bid = float(getattr(quote, "bid", 0.0) or 0.0)
        ask = float(getattr(quote, "ask", 0.0) or 0.0)
        out.append(
            {
                "symbol": leg.tradingsymbol,
                "kind": leg.instrument_type,
                "strike": leg.strike,
                "dir": leg.direction,
                "mid": round(leg.premium, 2),
                "bid": round(bid, 2),
                "ask": round(ask, 2),
                "iv": None if iv is None else round(iv, 4),
                "expiry": chain.expiry.date().isoformat(),
                "two_sided": bid > 0.0 and ask > 0.0,
            }
        )
    return out


def realistic_price(leg: dict[str, Any]) -> float:
    """Entry fill a taker gets: SELL at bid, BUY at ask; falls back to mid when a side is empty."""
    px = leg["bid"] if leg["dir"] == "SELL" else leg["ask"]
    return float(px if px > 0.0 else leg["mid"])


def credits(rows: list[dict[str, Any]]) -> tuple[float, float]:
    """(credit_mid, credit_realistic) per share; positive = net credit."""
    mid = sum((1.0 if r["dir"] == "SELL" else -1.0) * r["mid"] for r in rows)
    real = sum((1.0 if r["dir"] == "SELL" else -1.0) * realistic_price(r) for r in rows)
    return round(mid, 2), round(real, 2)


def expiry_pnl(rows: list[dict[str, Any]], spot: float, credit_realistic: float) -> float:
    """Per-share P&L at expiry for same-expiry legs (intrinsic only), entry at realistic fills."""
    pnl = credit_realistic
    for r in rows:
        intrinsic = (
            max(spot - r["strike"], 0.0) if r["kind"] == "CE" else max(r["strike"] - spot, 0.0)
        )
        pnl += -intrinsic if r["dir"] == "SELL" else intrinsic
    return pnl


def max_loss_per_share(
    rows: list[dict[str, Any]], credit_realistic: float, *, calendar: bool
) -> float:
    """Worst expiry P&L (>=0). Calendars are not closed-form at near expiry, so their risk is the
    debit paid (a calendar cannot lose more than it cost)."""
    if calendar:
        return round(max(0.0, -credit_realistic), 2)
    strikes = [r["strike"] for r in rows]
    grid = [0.0, *strikes, max(strikes) * 10.0]
    worst = min(expiry_pnl(rows, s, credit_realistic) for s in grid)
    return round(max(0.0, -worst), 2)


def _write(row: dict[str, Any]) -> None:
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    with _OUT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


async def _build(
    desk: SellerDesk,
    structure: str,
    underlying: str,
    ivr: float | None,
    today: date,
    expiry_calendar: dict[str, list[date]],
) -> tuple[list[dict[str, Any]], float, str, int] | str | bool:
    """(legs, spot, expiry_iso, dte) / a reason string when the builder found no liquid legs /
    False when no chain was available. The caller retries both of the latter next cycle."""
    if structure == CALENDAR_TYPE:
        expiries = select_calendar_expiries(today, expiry_calendar, underlying)
        if expiries is None:
            return "no_calendar_expiries"
        near_dte, far_dte = compute_dte(expiries[0], today), compute_dte(expiries[1], today)
        tier2 = await desk._chain_fetcher.fetch_tier2(underlying, [near_dte, far_dte])
        near, far = tier2.get(near_dte), tier2.get(far_dte)
        if near is None or far is None:
            return False
        built = build_two_expiry_structure(structure, near, far, 1)
        if built is None:
            return "calendar_atm_leg_missing"
        protection, income = built  # calendar: protection = far legs, income = near legs
        return (
            leg_rows(income, near) + leg_rows(protection, far),
            near.spot,
            near.expiry.date().isoformat(),
            near.days_to_expiry,
        )
    lo, _hi = dte_band(structure, ivr=ivr)
    chain = (await desk._chain_fetcher.fetch_tier2(underlying, [lo])).get(lo)
    if chain is None:
        return False
    built = build_structure(structure, chain, 1, ivr=ivr)
    if built is None:
        return why_no_structure(chain, structure, ivr)
    protection, income = built
    return (
        leg_rows(income + protection, chain),
        chain.spot,
        chain.expiry.date().isoformat(),
        chain.days_to_expiry,
    )


async def record_cycle(
    desk: SellerDesk,
    *,
    underlying: str,
    regime: str,
    ivr: float | None,
    term_structure: str | None,
    vix: float | None,
    today: date,
    expiry_calendar: dict[str, list[date]],
) -> None:
    """Record today's snapshot of each configured structure once. Never raises."""
    try:
        cfg = _seller_settings.structure_shadow()
        if not cfg.get("enabled", False):
            return
        _seed_done_from_file()
        types = [t for t in cfg.get("types", DEFAULT_TYPES) if t in DEFAULT_TYPES]
        for structure in types:
            key = (today.isoformat(), underlying, structure)
            if key in _DONE:
                continue
            _DONE.add(key)  # claim first: concurrent cycles must not double-write
            try:
                built = await _build(desk, structure, underlying, ivr, today, expiry_calendar)
            except Exception as exc:  # noqa: BLE001 -- shadow must never affect the desk
                _DONE.discard(key)
                _LOG.warning(
                    "structure_shadow_failed underlying=%s structure=%s err=%s",
                    underlying,
                    structure,
                    exc,
                )
                continue
            if built is False:
                _DONE.discard(key)  # no chain right now -> retry next cycle
                continue
            if isinstance(built, str):
                # Not liquid / not buildable right now (e.g. a thin wing early in the session):
                # keep retrying every cycle so the day's snapshot is not lost, log the reason once.
                _DONE.discard(key)
                if key not in _SKIP_LOGGED:
                    _SKIP_LOGGED.add(key)
                    _LOG.info(
                        "structure_shadow_skip underlying=%s structure=%s reason=%s",
                        underlying,
                        structure,
                        built,
                    )
                continue
            rows, spot, expiry, dte = built  # type: ignore[misc]
            is_cal = structure == CALENDAR_TYPE
            credit_mid, credit_real = credits(rows)
            _write(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "date": today.isoformat(),
                    "underlying": underlying,
                    "structure": structure,
                    "spot": round(spot, 2),
                    "expiry": expiry,
                    "dte": dte,
                    "regime": regime,
                    "ivr": None if ivr is None else round(ivr, 1),
                    "vix": vix,
                    "term_structure": term_structure,
                    "legs": rows,
                    "credit_mid": credit_mid,
                    "credit_realistic": credit_real,
                    "max_loss": max_loss_per_share(rows, credit_real, calendar=is_cal),
                    "lot_size_unit": "per_share",
                }
            )
            _LOG.info(
                "structure_shadow underlying=%s structure=%s credit_mid=%.2f credit_real=%.2f",
                underlying,
                structure,
                credit_mid,
                credit_real,
            )
    except Exception as exc:  # noqa: BLE001 -- shadow must never affect the desk
        _LOG.warning("structure_shadow_failed underlying=%s err=%s", underlying, exc)
