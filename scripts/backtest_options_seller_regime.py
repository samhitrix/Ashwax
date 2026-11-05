"""
Historical validation — options seller desk low-IV adaptation (Phase 2 of
the low-IV redesign plan, see project plan / memory for Phase 1-4 context).

NOT a full multi-leg options backtest. No historical per-strike option-chain
data exists anywhere in this system: options_iv_history/options_vol_intel_history
(src/db/repositories/options.py) store only SCALAR daily metrics (ATM IV, IVR,
term structure) — never a full chain snapshot. True multi-leg P&L replay is
therefore not possible from data this system actually has.

What this script does instead: walks real historical index OHLCV day-by-day
(no lookahead — every regime/trend input at day N uses only bars <= N) through
the ACTUAL live functions this system uses (src/options/regime_classifier.py,
src/options/strategy_selector.py), and prices theoretical strikes/premiums via
Black-Scholes (src/options/greeks.py) off a flat-IV assumption (no skew, no
smile, no bid/ask). Every number this script prints is a THEORETICAL
reconstruction, not a chain-verified backtest — labeled as such everywhere in
the report and JSON output. Its job is to produce an honest, small-sample
signal about whether a proposed change is plausible, not to certify one.

DATA CAVEATS (read before trusting a result):
  - No historical India VIX series exists in this system's DB (checked: no
    ohlcv_candles rows for any INDIA VIX symbol variant). Falls back to a
    REALIZED-VOLATILITY PROXY (annualized rolling stdev of the underlying's
    own daily log returns, x100 to look VIX-like) wherever "vix" is needed —
    labeled `vix_proxy` everywhere, never presented as true VIX. Regime
    classification against a proxy will not exactly match what the live
    system saw on any given historical day.
  - ATM IV read from options_iv_history/options_vol_intel_history if that
    underlying has rows (checked at startup, reported in the coverage line);
    otherwise the same realized-vol proxy stands in for IV too. A run
    reporting "0 IV-history rows" is an honest, valid result — not a bug.
  - Strike selection for a target delta is a closed-form Black-Scholes
    inversion (see `_strike_for_delta`), not a real strike ladder — a real
    chain only offers strikes at fixed intervals near this theoretical price.
  - Trade outcomes are priced only at simulated expiry (spot at entry+DTE
    trading days), not path-dependent — no early assignment, no interim
    adjustment, no partial-book/stop-loss management modeled.

Usage:
    python scripts/backtest_options_seller_regime.py --from 2020-01-01 --to 2026-08-01
    python scripts/backtest_options_seller_regime.py --from 2020-01-01 --to 2026-08-01 --underlying NIFTY --json-out out.json
"""

from __future__ import annotations

# Standalone dev/backtest script: imports follow sys.path bootstrap (E402).
# ruff: noqa: E402
# mypy: ignore-errors
import argparse
import json
import math
import os
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")  # KITE_API_KEY etc. for --backfill's broker.get_ohlcv() call

from src.options.greeks import DEFAULT_RISK_FREE_RATE, bs_price, days_to_years
from src.options.regime_classifier import (
    classify_regime,
    mean_revert_score,
    trend_direction,
    trending_score,
    wilder_adx,
)
from src.options.strategy_selector import _MODERATE_TREND_BIAS_THRESHOLD

IST = "Asia/Kolkata"
_TREND_LOOKBACK = 20
_ADX_MIN_BARS = 29  # wilder_adx's own floor: period*2+1, period=14

# Two delta profiles compared in Analysis (b)/(c)/(d): today's live
# structure_builder.py default (25-delta short / 10-delta long protection)
# vs. a tighter, more-ATM alternative for thin-premium regimes.
_WIDE_PROFILE = {"short": 0.25, "long": 0.10}
_TIGHT_PROFILE = {"short": 0.35, "long": 0.15}

# DTE bands compared in Analysis (d): current live default for
# bull_put_spread/bear_call_spread (options_spec.md §6 -> dte_ranges config,
# midpoint of 21-45) vs. a shorter-DTE alternative under test.
_CURRENT_DTE = 30
_SHORT_DTE = 14


# ── pure functions (unit-tested without a DB — see tests/unit/scripts/) ────


def realized_vol_proxy(closes: list[float], window: int = 20) -> list[float | None]:
    """Annualized rolling stdev of daily log returns, x100 (VIX-like scale).

    Stand-in for India VIX, which has no historical series in this system's
    DB (see module DATA CAVEATS). None for the first `window` bars."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < window + 1:
        return out
    log_rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
    for i in range(window, n):
        tail = log_rets[i - window : i]
        sd = statistics.pstdev(tail)
        out[i] = sd * math.sqrt(252) * 100.0
    return out


@dataclass
class RegimeDay:
    idx: int
    regime: str
    vix_proxy: float
    adx: float
    trend_score: float
    trend_direction: float | None
    allowed_selling: bool
    allowed_directional: bool


def compute_regime_timeline(
    closes: list[float], highs: list[float], lows: list[float]
) -> list[RegimeDay]:
    """Walk-forward regime classification, no lookahead: day i's regime uses
    only bars[0:i+1] — the exact same function (classify_regime) the live
    seller desk calls every 5 minutes, just fed historical closes instead of
    a live feed."""
    n = len(closes)
    vix_proxy = realized_vol_proxy(closes)
    out: list[RegimeDay] = []
    warmup = max(_ADX_MIN_BARS, _TREND_LOOKBACK)
    for i in range(warmup, n):
        if vix_proxy[i] is None:
            continue
        adx = wilder_adx(highs[: i + 1], lows[: i + 1], closes[: i + 1])
        if adx is None:
            continue
        tscore = trending_score(closes[: i + 1], lookback=_TREND_LOOKBACK)
        if tscore is None:
            continue
        tdir = trend_direction(closes[: i + 1], lookback=_TREND_LOOKBACK)
        revert = mean_revert_score(tscore)
        result = classify_regime(vix_proxy[i], adx, tscore, revert)
        out.append(
            RegimeDay(
                idx=i,
                regime=result.regime,
                vix_proxy=vix_proxy[i],
                adx=adx,
                trend_score=tscore,
                trend_direction=tdir,
                allowed_selling=result.allowed_selling,
                allowed_directional=result.allowed_directional,
            )
        )
    return out


def analyze_undefined_zone(timeline: list[RegimeDay]) -> dict[str, Any]:
    """(a) Is the UNDEFINED regime bucket actually empty of edge, or does it
    mostly already have a real signed trend the existing directional-debit
    override (strategy_selector._MODERATE_TREND_BIAS_THRESHOLD) would catch?
    """
    undefined = [d for d in timeline if d.regime == "UNDEFINED"]
    n = len(timeline)
    if not undefined:
        return {"n_total": n, "n_undefined": 0, "undefined_pct": 0.0, "n_with_trend": 0}
    with_trend = [
        d
        for d in undefined
        if d.trend_direction is not None
        and abs(d.trend_direction) >= _MODERATE_TREND_BIAS_THRESHOLD
    ]
    return {
        "n_total": n,
        "n_undefined": len(undefined),
        "undefined_pct": round(100.0 * len(undefined) / n, 1) if n else 0.0,
        "n_with_trend": len(with_trend),
        "with_trend_pct_of_undefined": (
            round(100.0 * len(with_trend) / len(undefined), 1) if undefined else 0.0
        ),
    }


def _strike_for_delta(spot: float, iv: float, t: float, r: float, delta: float, kind: str) -> float:
    """Closed-form Black-Scholes strike for a target |delta| in (0, 1)
    (theoretical — a real chain only offers strikes at fixed intervals near
    this price, see module DATA CAVEATS).

    Call delta = N(d1) -> d1 = invcdf(delta) (negative for OTM delta<0.5,
    which correctly pushes the strike ABOVE spot below). Put delta =
    N(d1)-1, so |put delta|=delta -> N(d1)=1-delta -> d1 = invcdf(1-delta)
    (positive for OTM delta<0.5, correctly pushing the strike BELOW spot).
    """
    nd = statistics.NormalDist()
    d1 = nd.inv_cdf(delta) if kind == "CE" else nd.inv_cdf(1.0 - delta)
    return spot * math.exp((r + 0.5 * iv * iv) * t - d1 * iv * math.sqrt(t))


@dataclass
class SpreadTrial:
    entry_idx: int
    expiry_idx: int
    side: str  # "bull_put" | "bear_call"
    premium: float
    max_loss: float
    pnl: float
    win: bool


def _price_vertical(
    spot_entry: float,
    spot_expiry: float,
    iv: float,
    t: float,
    r: float,
    side: str,
    profile: dict[str, float],
) -> tuple[float, float, float]:
    """(premium_collected, max_loss, pnl_at_expiry) for one credit vertical,
    priced entirely off spot_entry/iv at entry, settled at spot_expiry."""
    kind = "PE" if side == "bull_put" else "CE"
    short_k = _strike_for_delta(spot_entry, iv, t, r, profile["short"], kind)
    long_k = _strike_for_delta(spot_entry, iv, t, r, profile["long"], kind)
    if side == "bull_put":
        short_k, long_k = max(short_k, long_k), min(short_k, long_k)
    else:
        short_k, long_k = min(short_k, long_k), max(short_k, long_k)
    short_prem = bs_price(spot_entry, short_k, t, r, iv, kind)
    long_prem = bs_price(spot_entry, long_k, t, r, iv, kind)
    premium = short_prem - long_prem
    width = abs(short_k - long_k)
    max_loss = max(width - premium, 0.0)
    if side == "bull_put":
        loss = max(short_k - spot_expiry, 0.0) - max(long_k - spot_expiry, 0.0)
    else:
        loss = max(spot_expiry - short_k, 0.0) - max(spot_expiry - long_k, 0.0)
    pnl = premium - min(loss, width)
    return premium, max_loss, pnl


def run_spread_trials(
    timeline: list[RegimeDay],
    closes: list[float],
    ivs: list[float],
    *,
    dte: int,
    profile: dict[str, float],
    side_selector: str,  # "abs_premium" | "trend_matched"
) -> list[SpreadTrial]:
    """(b)/(c)/(d) shared engine: for every day the live menu would allow a
    single-sided credit spread (bull_put_spread/bear_call_spread —
    regime-agnostic per strategy_selector._MENU), simulate both sides
    theoretically and pick one per `side_selector`, then settle at
    entry_idx+dte using the REAL historical close dte trading days later.
    """
    r = DEFAULT_RISK_FREE_RATE
    t = days_to_years(dte)
    trials: list[SpreadTrial] = []
    for day in timeline:
        i = day.idx
        expiry_idx = i + dte
        if expiry_idx >= len(closes):
            continue
        iv = ivs[i]
        if iv is None or iv <= 0:
            continue
        spot_entry = closes[i]
        spot_expiry = closes[expiry_idx]
        iv_frac = iv / 100.0
        bp_prem, bp_loss, bp_pnl = _price_vertical(
            spot_entry, spot_expiry, iv_frac, t, r, "bull_put", profile
        )
        bc_prem, bc_loss, bc_pnl = _price_vertical(
            spot_entry, spot_expiry, iv_frac, t, r, "bear_call", profile
        )
        if side_selector == "trend_matched" and day.trend_direction is not None:
            side = "bull_put" if day.trend_direction > 0 else "bear_call"
        else:
            side = "bull_put" if bp_prem >= bc_prem else "bear_call"
        premium, max_loss, pnl = (
            (bp_prem, bp_loss, bp_pnl) if side == "bull_put" else (bc_prem, bc_loss, bc_pnl)
        )
        trials.append(
            SpreadTrial(
                entry_idx=i,
                expiry_idx=expiry_idx,
                side=side,
                premium=premium,
                max_loss=max_loss,
                pnl=pnl,
                win=pnl > 0,
            )
        )
    return trials


def summarize_trials(trials: list[SpreadTrial], *, dte: int) -> dict[str, Any]:
    n = len(trials)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for t in trials if t.win)
    avg_premium = sum(t.premium for t in trials) / n
    avg_pnl = sum(t.pnl for t in trials) / n
    return {
        "n": n,
        "win_rate_pct": round(100.0 * wins / n, 1),
        "avg_premium": round(avg_premium, 2),
        "avg_pnl": round(avg_pnl, 2),
        "avg_premium_per_day_of_risk": round(avg_premium / dte, 4),
    }


# ── all-structures EV calibration (Options learning-engine PR1) ────────────
#
# Extends the low-IV validation's proven theoretical-BS methodology to every
# structure type the seller-desk menu (src/options/strategy_selector.py) and
# the buyer-first engine (src/options/structure_selector.py) can produce, so
# a real ev_calibration config block (avg_win/avg_loss/win_rate per type,
# PR2) can replace today's universal 0.5x/0.6x/1.5x heuristic multiplier in
# seller_desk.py's live EV gate. Same DATA CAVEATS as above: theoretical
# Black-Scholes reconstructions, not chain-verified.

# Deltas for the two DEBIT verticals (bull_call_spread/bear_put_spread) — no
# equivalent already existed in this script (only the credit-vertical WIDE/
# TIGHT profiles above); near-ATM long leg, OTM short leg for the spread's cap.
_DEBIT_PROFILE = {"long": 0.45, "short": 0.20}

_ALL_STRUCTURE_TYPES = (
    "bull_put_spread",
    "bear_call_spread",
    "iron_condor",
    "iron_butterfly",
    "long_straddle",
    "long_strangle",
    "bull_call_spread",
    "bear_put_spread",
    "long_call",
    "protective_put",
)


def _strikes_for_structure(
    structure_type: str, spot: float, iv: float, t: float, r: float, profile: dict[str, float]
) -> list[tuple[str, str, float]]:
    """Per-type (kind, direction, strike) leg layout, every strike via the
    existing _strike_for_delta — theoretical, same model as the rest of this
    script. `protective_put` is NOT handled here (see _price_protective_put —
    it combines an equity leg with a put, not an options-only combo)."""

    def sf(delta: float, kind: str) -> float:
        return _strike_for_delta(spot, iv, t, r, delta, kind)

    if structure_type == "bull_put_spread":
        short_k, long_k = sf(profile["short"], "PE"), sf(profile["long"], "PE")
        short_k, long_k = max(short_k, long_k), min(short_k, long_k)
        return [("PE", "SELL", short_k), ("PE", "BUY", long_k)]
    if structure_type == "bear_call_spread":
        short_k, long_k = sf(profile["short"], "CE"), sf(profile["long"], "CE")
        short_k, long_k = min(short_k, long_k), max(short_k, long_k)
        return [("CE", "SELL", short_k), ("CE", "BUY", long_k)]
    if structure_type == "bull_call_spread":
        long_k, short_k = sf(_DEBIT_PROFILE["long"], "CE"), sf(_DEBIT_PROFILE["short"], "CE")
        long_k, short_k = min(long_k, short_k), max(long_k, short_k)
        return [("CE", "BUY", long_k), ("CE", "SELL", short_k)]
    if structure_type == "bear_put_spread":
        long_k, short_k = sf(_DEBIT_PROFILE["long"], "PE"), sf(_DEBIT_PROFILE["short"], "PE")
        long_k, short_k = max(long_k, short_k), min(long_k, short_k)
        return [("PE", "BUY", long_k), ("PE", "SELL", short_k)]
    if structure_type == "long_straddle":
        atm_k = sf(0.50, "CE")
        return [("CE", "BUY", atm_k), ("PE", "BUY", atm_k)]
    if structure_type == "long_strangle":
        return [("CE", "BUY", sf(0.30, "CE")), ("PE", "BUY", sf(0.30, "PE"))]
    if structure_type == "iron_condor":
        return _strikes_for_structure(
            "bull_put_spread", spot, iv, t, r, profile
        ) + _strikes_for_structure("bear_call_spread", spot, iv, t, r, profile)
    if structure_type == "iron_butterfly":
        return [
            ("PE", "SELL", sf(0.50, "PE")),
            ("CE", "SELL", sf(0.50, "CE")),
            ("PE", "BUY", sf(profile["long"], "PE")),
            ("CE", "BUY", sf(profile["long"], "CE")),
        ]
    if structure_type == "long_call":
        return [("CE", "BUY", sf(0.50, "CE"))]
    raise ValueError(f"unsupported structure_type: {structure_type}")


def _price_combo(
    legs: list[tuple[str, str, float]],
    spot_entry: float,
    spot_expiry: float,
    iv: float,
    t: float,
    r: float,
) -> tuple[float, float, float]:
    """(net_premium, max_loss, pnl) for an arbitrary options-only combo — each
    leg priced independently via bs_price() and summed (flat-IV, no cross-leg
    correlation model — matches every other pricing path in this codebase's
    live code, so a fancier backtest model here would be internally
    inconsistent with what it's calibrating). net_premium > 0 = net debit
    (paid), < 0 = net credit (collected). max_loss is computed empirically —
    worst realized pnl over a wide grid of terminal spots plus every leg's own
    strike — rather than a separate closed-form formula per structure shape,
    since every structure built here is defined-risk by construction (this
    codebase's avoid_naked_selling philosophy) so the true worst case is
    bounded and this grid always finds it.
    """

    def _pnl_at(spot: float) -> float:
        total = 0.0
        for kind, direction, strike in legs:
            intrinsic = max((spot - strike) if kind == "CE" else (strike - spot), 0.0)
            premium = bs_price(spot_entry, strike, t, r, iv, kind)
            sign = 1.0 if direction == "BUY" else -1.0
            total += sign * (intrinsic - premium)
        return total

    net_premium = sum(
        (1.0 if d == "BUY" else -1.0) * bs_price(spot_entry, k, t, r, iv, kd) for kd, d, k in legs
    )
    grid = [spot_entry * f for f in (0.01, 0.5, 0.7, 0.85, 0.95, 1.0, 1.05, 1.15, 1.3, 1.5, 3.0)]
    grid += [k for _, _, k in legs]
    worst_pnl = min(_pnl_at(s) for s in grid)
    max_loss = max(0.0, -worst_pnl)
    pnl = _pnl_at(spot_expiry)
    return net_premium, max_loss, pnl


def _price_protective_put(
    spot_entry: float, spot_expiry: float, iv: float, t: float, r: float, put_delta: float
) -> tuple[float, float, float]:
    """(put_premium, max_loss, pnl) for equity + long OTM put hedge. max_loss
    mirrors src.options.structure_selector._protective_put's live formula
    exactly (downside-to-strike + put premium, per share) so the theoretical
    backtest number and the live formula are apples-to-apples."""
    put_k = _strike_for_delta(spot_entry, iv, t, r, put_delta, "PE")
    put_premium = bs_price(spot_entry, put_k, t, r, iv, "PE")
    put_intrinsic = max(put_k - spot_expiry, 0.0)
    pnl = (spot_expiry - spot_entry) + (put_intrinsic - put_premium)
    max_loss = max(spot_entry - put_k, 0.0) + put_premium
    return put_premium, max_loss, pnl


def run_structure_trials(
    timeline: list[RegimeDay],
    closes: list[float],
    ivs: list[float | None],
    *,
    structure_type: str,
    dte: int,
    profile: dict[str, float],
) -> list[SpreadTrial]:
    """Generalizes run_spread_trials to any of _ALL_STRUCTURE_TYPES via
    _strikes_for_structure/_price_combo (or _price_protective_put) — same
    walk-forward, no-lookahead loop shape as the original two-type version."""
    r = DEFAULT_RISK_FREE_RATE
    t = days_to_years(dte)
    trials: list[SpreadTrial] = []
    for day in timeline:
        i = day.idx
        expiry_idx = i + dte
        if expiry_idx >= len(closes):
            continue
        iv = ivs[i]
        if iv is None or iv <= 0:
            continue
        spot_entry = closes[i]
        spot_expiry = closes[expiry_idx]
        iv_frac = iv / 100.0
        try:
            if structure_type == "protective_put":
                premium, max_loss, pnl = _price_protective_put(
                    spot_entry, spot_expiry, iv_frac, t, r, profile.get("long", 0.25)
                )
            else:
                legs = _strikes_for_structure(structure_type, spot_entry, iv_frac, t, r, profile)
                premium, max_loss, pnl = _price_combo(legs, spot_entry, spot_expiry, iv_frac, t, r)
        except (ValueError, ZeroDivisionError):
            continue
        trials.append(
            SpreadTrial(
                entry_idx=i,
                expiry_idx=expiry_idx,
                side=structure_type,
                premium=premium,
                max_loss=max_loss,
                pnl=pnl,
                win=pnl > 0,
            )
        )
    return trials


def summarize_structure_trials(trials: list[SpreadTrial]) -> dict[str, Any]:
    """Like summarize_trials, but splits avg_win/avg_loss (mean pnl of winners
    vs losers separately) instead of one avg_pnl — pop_calibration never
    needed this split, but ev_calibration's EV formula does."""
    n = len(trials)
    if n == 0:
        return {"n": 0}
    wins = [tr for tr in trials if tr.win]
    losses = [tr for tr in trials if not tr.win]
    return {
        "n": n,
        "win_rate_pct": round(100.0 * len(wins) / n, 1),
        "avg_win": round(sum(tr.pnl for tr in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(abs(tr.pnl) for tr in losses) / len(losses), 2) if losses else 0.0,
        "avg_premium": round(sum(tr.premium for tr in trials) / n, 2),
    }


async def run_all_structures(
    pool: asyncpg.Pool, underlying: str, dte_by_type: dict[str, int]
) -> dict[str, Any]:
    """--all-structures: theoretical win_rate/avg_win/avg_loss for every type
    in _ALL_STRUCTURE_TYPES, one underlying, at that type's live DTE default.
    Output is a human-reviewed artifact for PR2's config/options_settings.json
    ev_calibration block — never auto-applied."""
    candidates = [underlying, f"NSE:{underlying}", f"{underlying} 50", f"NSE:{underlying} 50"]
    if underlying == "BANKNIFTY":
        candidates = ["BANKNIFTY", "NSE:BANKNIFTY", "NIFTY BANK", "NSE:NIFTY BANK", *candidates]
    elif underlying == "FINNIFTY":
        candidates = [
            "FINNIFTY",
            "NSE:FINNIFTY",
            "NIFTY FIN SERVICE",
            "NSE:NIFTY FIN SERVICE",
            *candidates,
        ]
    sym, _dates, closes, highs, lows = await _fetch_ohlcv(pool, candidates)
    out: dict[str, Any] = {"ohlcv_symbol": sym, "underlying": underlying}
    if not closes:
        return out
    timeline = compute_regime_timeline(closes, highs, lows)
    if not timeline:
        return out
    vix_proxy_by_idx = {d.idx: d.vix_proxy for d in timeline}
    ivs: list[float | None] = [vix_proxy_by_idx.get(i) for i in range(len(closes))]
    for structure_type in _ALL_STRUCTURE_TYPES:
        dte = dte_by_type.get(structure_type, _CURRENT_DTE)
        profile = _WIDE_PROFILE
        trials = run_structure_trials(
            timeline, closes, ivs, structure_type=structure_type, dte=dte, profile=profile
        )
        out[structure_type] = summarize_structure_trials(trials)
    return out


# ── DB-driving orchestration ────────────────────────────────────────────────


def db_url() -> str:
    if os.environ.get("DB_URL"):
        return os.environ["DB_URL"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("DB_URL="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("DB_URL not found in environment or .env")


def parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


async def backfill_index_ohlcv(pool: asyncpg.Pool, underlying: str, days: int) -> int:
    """One-time bootstrap: this system has never ingested daily INDEX OHLCV
    (only equity/stock symbols get daily candles from the regular swing scan
    universe -- confirmed 2026-09-04, zero ohlcv_candles rows for
    NIFTY/BANKNIFTY/FINNIFTY/VIX under any symbol convention). Fetches once
    via the same broker.get_ohlcv() interface the live seller-desk loop
    already calls for its intraday regime inputs (src/orchestrator.py
    _seller_desk_loop), then stores under the bare `underlying` symbol via
    OHLCVRepository.upsert_candles (idempotent, ON CONFLICT DO UPDATE) so
    later runs of this script need no re-fetch. Kite's daily-interval
    historical API tops out around ~2000 days per call; deeper history
    would need a chunked backfill, not built here (unneeded for regime
    calibration purposes -- ~5.5 years is already a large sample)."""
    from src.brokers.adapters.kite.adapter import KiteAdapter
    from src.db.repositories.ohlcv import OHLCVRepository
    from src.options.nfo_chain import index_spot_symbol

    broker = KiteAdapter.from_env()
    df = await broker.get_ohlcv(f"NSE:{index_spot_symbol(underlying)}", "day", days)
    if df is None or df.empty:
        return 0
    rows = [
        {
            "ts": row["date"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row.get("volume", 0),
        }
        for _, row in df.iterrows()
    ]
    repo = OHLCVRepository(pool)
    return await repo.upsert_candles(underlying, "day", rows)


async def _check_iv_history_coverage(pool: asyncpg.Pool, underlying: str) -> dict[str, Any]:
    async with pool.acquire() as conn:
        iv_count = await conn.fetchval(
            "SELECT COUNT(*) FROM options_iv_history WHERE underlying = $1", underlying
        )
        vi_count = await conn.fetchval(
            "SELECT COUNT(*) FROM options_vol_intel_history WHERE symbol = $1", underlying
        )
    return {
        "underlying": underlying,
        "options_iv_history_rows": int(iv_count or 0),
        "options_vol_intel_history_rows": int(vi_count or 0),
    }


async def _fetch_ohlcv(
    pool: asyncpg.Pool, symbol_candidates: list[str]
) -> tuple[str, list, list, list]:
    """Try each candidate symbol in order (index OHLCV storage convention
    isn't guaranteed identical across ingestion history); return the first
    with any rows, plus (dates, closes, highs, lows) sorted ascending."""
    async with pool.acquire() as conn:
        for sym in symbol_candidates:
            rows = await conn.fetch(
                "SELECT ts, open, high, low, close FROM ohlcv_candles "
                "WHERE symbol=$1 AND timeframe='day' ORDER BY ts ASC",
                sym,
            )
            if rows:
                closes = [float(r["close"]) for r in rows]
                highs = [float(r["high"]) for r in rows]
                lows = [float(r["low"]) for r in rows]
                dates = [r["ts"] for r in rows]
                return sym, dates, closes, highs, lows
    return "", [], [], [], []


def print_report(underlying: str, coverage: dict[str, Any], results: dict[str, Any]) -> None:
    print(f"\n=== Options seller desk low-IV validation — {underlying} ===")
    print(
        f"  IV-history coverage: {coverage['options_iv_history_rows']} rows "
        f"(options_iv_history), {coverage['options_vol_intel_history_rows']} rows "
        f"(options_vol_intel_history)"
    )
    print(f"  OHLCV symbol used: {results.get('ohlcv_symbol') or '(none found)'}")
    print(f"  Regime-timeline days: {results.get('timeline_n', 0)}")
    if results.get("timeline_n", 0) == 0:
        print(
            "  No usable OHLCV history — every analysis below is N=0. "
            "This is an honest result, not a bug (see module DATA CAVEATS)."
        )
        return

    a = results["analysis_a"]
    print(
        f"\n  (a) UNDEFINED regime zone: {a['n_undefined']}/{a['n_total']} days "
        f"({a['undefined_pct']}%) — of those, {a.get('n_with_trend', 0)} "
        f"({a.get('with_trend_pct_of_undefined', 0)}%) already have a signed trend beyond "
        f"the existing directional-debit override threshold."
    )

    for label, key in (("(b) wide (25/10)", "b_wide"), ("(b) tight (35/15)", "b_tight")):
        s = results[key]
        print(
            f"  {label}: n={s.get('n', 0)} win_rate={s.get('win_rate_pct', '-')}% "
            f"avg_premium={s.get('avg_premium', '-')} "
            f"avg_premium/day={s.get('avg_premium_per_day_of_risk', '-')}"
        )

    for label, key in (
        ("(c) abs-premium (live logic)", "c_abs_premium"),
        ("(c) trend-matched", "c_trend_matched"),
    ):
        s = results[key]
        print(
            f"  {label}: n={s.get('n', 0)} win_rate={s.get('win_rate_pct', '-')}% "
            f"avg_pnl={s.get('avg_pnl', '-')}"
        )

    for label, key in (
        ("(d) current DTE (30)", "d_current_dte"),
        ("(d) short DTE (14)", "d_short_dte"),
    ):
        s = results[key]
        print(
            f"  {label}: n={s.get('n', 0)} win_rate={s.get('win_rate_pct', '-')}% "
            f"avg_premium/day={s.get('avg_premium_per_day_of_risk', '-')}"
        )

    print(
        "\n  ALL numbers above are theoretical Black-Scholes reconstructions off a "
        "flat-IV / realized-vol-proxy assumption, not a chain-verified backtest — "
        "see the module docstring DATA CAVEATS before acting on any of this."
    )


async def run_validation(
    pool: asyncpg.Pool, underlying: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    coverage = await _check_iv_history_coverage(pool, underlying)

    candidates = [underlying, f"NSE:{underlying}", f"{underlying} 50", f"NSE:{underlying} 50"]
    if underlying == "NIFTY":
        candidates = ["NIFTY", "NSE:NIFTY", "NIFTY 50", "NSE:NIFTY 50", *candidates]
    elif underlying == "BANKNIFTY":
        candidates = ["BANKNIFTY", "NSE:BANKNIFTY", "NIFTY BANK", "NSE:NIFTY BANK", *candidates]
    elif underlying == "FINNIFTY":
        candidates = [
            "FINNIFTY",
            "NSE:FINNIFTY",
            "NIFTY FIN SERVICE",
            "NSE:NIFTY FIN SERVICE",
            *candidates,
        ]

    sym, _dates, closes, highs, lows = await _fetch_ohlcv(pool, candidates)
    results: dict[str, Any] = {"ohlcv_symbol": sym, "timeline_n": 0}
    if not closes:
        return coverage, results

    timeline = compute_regime_timeline(closes, highs, lows)
    results["timeline_n"] = len(timeline)
    if not timeline:
        return coverage, results

    # ATM IV proxy: options_vol_intel_history if this underlying has rows
    # (checked in `coverage`), else the same realized-vol proxy used for
    # regime classification (see module DATA CAVEATS).
    vix_proxy_by_idx = {d.idx: d.vix_proxy for d in timeline}
    ivs: list[float | None] = [vix_proxy_by_idx.get(i) for i in range(len(closes))]

    results["analysis_a"] = analyze_undefined_zone(timeline)

    # bull_put_spread/bear_call_spread are regime-agnostic in the live menu
    # (strategy_selector._menu()'s valid_regimes=None for both) -- every
    # timeline day is a candidate, not just non-UNDEFINED ones.
    sellable = timeline
    b_wide = run_spread_trials(
        sellable, closes, ivs, dte=_CURRENT_DTE, profile=_WIDE_PROFILE, side_selector="abs_premium"
    )
    b_tight = run_spread_trials(
        sellable, closes, ivs, dte=_CURRENT_DTE, profile=_TIGHT_PROFILE, side_selector="abs_premium"
    )
    results["b_wide"] = summarize_trials(b_wide, dte=_CURRENT_DTE)
    results["b_tight"] = summarize_trials(b_tight, dte=_CURRENT_DTE)

    c_abs = b_wide  # same trial set as (b) wide/abs_premium — reuse, don't recompute
    c_trend = run_spread_trials(
        sellable,
        closes,
        ivs,
        dte=_CURRENT_DTE,
        profile=_WIDE_PROFILE,
        side_selector="trend_matched",
    )
    results["c_abs_premium"] = summarize_trials(c_abs, dte=_CURRENT_DTE)
    results["c_trend_matched"] = summarize_trials(c_trend, dte=_CURRENT_DTE)

    d_current = b_wide
    d_short = run_spread_trials(
        sellable, closes, ivs, dte=_SHORT_DTE, profile=_WIDE_PROFILE, side_selector="abs_premium"
    )
    results["d_current_dte"] = summarize_trials(d_current, dte=_CURRENT_DTE)
    results["d_short_dte"] = summarize_trials(d_short, dte=_SHORT_DTE)

    return coverage, results


async def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--from",
        dest="from_",
        metavar="YYYY-MM-DD",
        help="unused today (OHLCV loaded in full, filtered by symbol only) — reserved for a future windowed run",
    )
    ap.add_argument("--to", metavar="YYYY-MM-DD", help="unused today, see --from")
    ap.add_argument("--underlying", default="NIFTY", choices=["NIFTY", "BANKNIFTY", "FINNIFTY"])
    ap.add_argument(
        "--backfill",
        action="store_true",
        help="one-time fetch of daily index OHLCV via the live broker before validating "
        "(this system has never stored index-level daily candles — see backfill_index_ohlcv "
        "docstring); requires live Kite credentials, safe to re-run (idempotent upsert)",
    )
    ap.add_argument(
        "--backfill-days", type=int, default=2000, help="Kite daily-interval cap is ~2000"
    )
    ap.add_argument("--json-out", type=Path)
    ap.add_argument(
        "--all-structures",
        action="store_true",
        help="Options learning-engine PR1: instead of the low-IV validation report, run "
        "theoretical win_rate/avg_win/avg_loss for every structure type "
        "(_ALL_STRUCTURE_TYPES) at its live DTE default — the artifact PR2 hand-populates "
        "config/options_settings.json ev_calibration from, after human review (never "
        "auto-applied). Combine with --underlying to run one index at a time.",
    )
    args = ap.parse_args()

    pool = await asyncpg.create_pool(db_url(), min_size=1, max_size=4)
    try:
        if args.backfill:
            n = await backfill_index_ohlcv(pool, args.underlying, args.backfill_days)
            print(f"  backfilled {n} daily candles for {args.underlying}")
        if args.all_structures:
            from src.options import seller_settings as _seller_settings

            _dte_cfg = _seller_settings.dte_ranges()  # {structure_type: [min_dte, max_dte]}
            dte_by_type = {
                t: int(sum(r[:2]) / 2) if isinstance(r, list) and len(r) >= 2 else _CURRENT_DTE
                for t, r in _dte_cfg.items()
            }
            structure_results = await run_all_structures(pool, args.underlying, dte_by_type)
        else:
            coverage, results = await run_validation(pool, args.underlying)
    finally:
        await pool.close()

    if args.all_structures:
        print(f"\n=== Options EV calibration (theoretical) — {args.underlying} ===")
        print(f"  OHLCV symbol used: {structure_results.get('ohlcv_symbol') or '(none found)'}")
        for st in _ALL_STRUCTURE_TYPES:
            s = structure_results.get(st, {})
            print(
                f"  {st}: n={s.get('n', 0)} win_rate={s.get('win_rate_pct', '-')}% "
                f"avg_win={s.get('avg_win', '-')} avg_loss={s.get('avg_loss', '-')}"
            )
        print(
            "\n  ALL numbers above are theoretical Black-Scholes reconstructions, not a "
            "chain-verified backtest — see the module docstring DATA CAVEATS. Review before "
            "hand-populating config/options_settings.json ev_calibration."
        )
        if args.json_out:
            args.json_out.write_text(
                json.dumps(
                    {"underlying": args.underlying, "structures": structure_results},
                    default=str,
                    indent=2,
                )
            )
            print(f"\n  full output written to {args.json_out}")
        return

    print_report(args.underlying, coverage, results)
    if args.json_out:
        args.json_out.write_text(
            json.dumps(
                {"underlying": args.underlying, "coverage": coverage, "results": results},
                default=str,
                indent=2,
            )
        )
        print(f"\n  full output written to {args.json_out}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
