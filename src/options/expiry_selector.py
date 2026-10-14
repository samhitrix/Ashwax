"""Seller-desk expiry selection + pre-entry DTE rules (options_spec.md §6).

Excludes the expiry_guardian safety net (forced closes at 3/2/1/0 DTE) —
that's a separate module with real capital consequences, built at the
execution-layer checkpoint alongside the leg executor and margin monitor.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.options import seller_settings as _seller_settings

_BASE = Path(__file__).parent.parent.parent
_EXPIRY_CALENDAR_PATH = _BASE / "config" / "nse_expiry_calendar.json"

# Optimal DTE ranges per strategy type (options_spec.md §6 table). Defaults;
# config (options_settings.json dte_ranges) overrides when present -- read
# fresh on every call (see _dte_ranges()), not cached at import time.
_DEFAULT_DTE_RANGES: dict[str, tuple[int, int]] = {
    "iron_condor": (30, 45),
    "iron_butterfly": (14, 21),
    "long_straddle": (7, 21),
    "long_strangle": (7, 21),
    "bull_put_spread": (21, 45),
    "bear_call_spread": (21, 45),
    "bull_call_spread": (21, 45),
    "bear_put_spread": (21, 45),
}

_DEFAULT_MIN_ENTRY_DTE = 2


def _dte_ranges() -> dict[str, tuple[int, int]]:
    cfg = _seller_settings.dte_ranges()
    entries = {k: v for k, v in cfg.items() if not k.startswith("_") and isinstance(v, list)}
    if not entries:
        return _DEFAULT_DTE_RANGES
    return {k: (int(v[0]), int(v[1])) for k, v in entries.items()}


def _min_entry_dte_default() -> int:
    val = _seller_settings.expiry_rules().get("min_entry_dte")
    return int(val) if val is not None else _DEFAULT_MIN_ENTRY_DTE


_LOW_IVR_DTE_TYPES = frozenset({"bull_put_spread", "bear_call_spread"})


def dte_band(strategy_type: str, *, ivr: float | None = None) -> tuple[int, int]:
    """Optimal (min_dte, max_dte) for a strategy type (§6 table); (0, 999)
    for any type not in the table (no band restriction).

    ``ivr``, when options_settings.json's low_ivr_mode is enabled and
    cleared, swaps bull_put_spread/bear_call_spread onto a shorter DTE band
    (data-informed 2026-09-04 -- see low_ivr_mode's own _note) -- off by
    default and scoped to the same two types low_ivr_mode's delta tiering
    applies to (see structure_builder._low_ivr_profile).
    """
    if ivr is not None and strategy_type in _LOW_IVR_DTE_TYPES:
        cfg = _seller_settings.low_ivr_mode()
        if cfg.get("enabled", False) and ivr < float(cfg.get("ivr_threshold", 25.0)):
            band = cfg.get("dte_range")
            if isinstance(band, list) and len(band) == 2:
                return int(band[0]), int(band[1])
    return _dte_ranges().get(strategy_type, (0, 999))


def load_expiry_calendar(path: Path | None = None) -> dict[str, list[date]]:
    """Underscore-prefixed / non-list keys (``_comment`` etc.) are ignored."""
    raw = json.loads((path or _EXPIRY_CALENDAR_PATH).read_text(encoding="utf-8"))
    return {
        key: [date.fromisoformat(d) for d in val]
        for key, val in raw.items()
        if not key.startswith("_") and isinstance(val, list)
    }


def next_expiry(underlying: str, today: date, calendar: dict[str, list[date]]) -> date | None:
    """Index-specific next expiry — never a generic 'next Thursday' guess (§6).

    ``underlying`` may carry an exchange prefix (e.g. "NSE:NIFTY", as passed
    by orchestrator.py's seller-desk loop) — the calendar JSON keys are bare
    roots, so the prefix must be stripped before lookup (same pattern as
    NFOChainProvider._root() in nfo_chain.py).
    """
    root = underlying.split(":")[-1].upper()
    future = sorted(d for d in calendar.get(root, []) if d >= today)
    return future[0] if future else None


def dte(expiry: date, today: date) -> int:
    return (expiry - today).days


def pre_entry_dte_check(
    underlying: str,
    today: date,
    calendar: dict[str, list[date]],
    *,
    min_dte: int | None = None,
) -> tuple[bool, str]:
    """(allowed, reason). Blocks entry if the index-specific DTE < min_dte.

    ``min_dte`` defaults to config (options_settings.json
    expiry_rules.min_entry_dte), read fresh -- not bound at function-def
    time, so a config change takes effect without a process restart.
    """
    resolved_min_dte = min_dte if min_dte is not None else _min_entry_dte_default()
    expiry = next_expiry(underlying, today, calendar)
    if expiry is None:
        return False, f"no future expiry in calendar for {underlying}"
    d = dte(expiry, today)
    if d < resolved_min_dte:
        return False, f"dte={d} < min_entry_dte={resolved_min_dte}"
    return True, f"dte={d}"


def pre_entry_dte_check_from_expiry(
    nearest_expiry: date,
    today: date,
    *,
    min_dte: int | None = None,
) -> tuple[bool, str]:
    """Same (allowed, reason) check as pre_entry_dte_check, but takes an
    already-known REAL nearest expiry (e.g. from a freshly fetched
    OptionChain) instead of looking one up in the calendar JSON.

    Found live 2026-09-10: nse_expiry_calendar.json is explicitly flagged in
    its own _VERIFY_BEFORE_ENABLE note as unverified WEEKDAY-GENERATED
    placeholder data, never replaced with real NSE circular dates. It still
    lists BANKNIFTY/FINNIFTY as weekly -- but the real NFO instrument dump
    (via ChainFetcher/NFOChainProvider, which this system's chain-fetching
    already correctly uses everywhere else) shows their nearest real expiry
    sitting ~19-20 days out, consistent with NSE's actual monthly-only
    policy for these two indices since 2023. pre_entry_dte_check() was the
    one place still trusting the stale calendar -- it would ALLOW entry
    against a fictional near-dated contract that doesn't exist in the real
    market, only for the later real chain fetch (DTE-band matching in
    run_cycle) to correctly find nothing and silently reject. This variant
    removes that disconnect by checking against the same real data source
    the rest of the pipeline already uses."""
    resolved_min_dte = min_dte if min_dte is not None else _min_entry_dte_default()
    d = dte(nearest_expiry, today)
    if d < resolved_min_dte:
        return False, f"dte={d} < min_entry_dte={resolved_min_dte}"
    return True, f"dte={d}"


def select_expiry(
    strategy_type: str,
    today: date,
    calendar: dict[str, list[date]],
    underlying: str,
    *,
    event_date: date | None = None,
) -> date | None:
    """Nearest available expiry within the strategy's optimal DTE band.

    For straddle/strangle, also requires expiry AFTER ``event_date`` when
    supplied — bracket the event, per §6.
    """
    lo, hi = _dte_ranges().get(strategy_type, (0, 999))
    candidates = calendar.get(underlying.split(":")[-1].upper(), [])
    in_band = [d for d in candidates if lo <= dte(d, today) <= hi]
    if strategy_type in ("long_straddle", "long_strangle") and event_date is not None:
        in_band = [d for d in in_band if d > event_date]
    if not in_band:
        return None
    return min(in_band, key=lambda d: dte(d, today))


def select_calendar_expiries(
    today: date, calendar: dict[str, list[date]], underlying: str
) -> tuple[date, date] | None:
    """(sell_expiry, buy_expiry) = (nearest weekly, nearest monthly) for a
    calendar spread. "Monthly" = the last expiry occurring in its calendar
    month among the available dates (standard NSE convention); "weekly" =
    the single nearest future expiry."""
    future = sorted(d for d in calendar.get(underlying.split(":")[-1].upper(), []) if d > today)
    if len(future) < 2:
        return None
    sell_expiry = future[0]
    monthly_by_ym: dict[tuple[int, int], date] = {}
    for d in future:
        key = (d.year, d.month)
        if key not in monthly_by_ym or d > monthly_by_ym[key]:
            monthly_by_ym[key] = d
    monthlies = sorted(v for v in monthly_by_ym.values() if v > sell_expiry)
    if not monthlies:
        return None
    return sell_expiry, monthlies[0]
