"""Seller-first engine config loader — config/options_settings.json (options_spec.md §21).

Cached-load pattern matching src/market/calendar.py. Scoped to what this
foundation sub-project needs: master switch, universe, chain-fetch cadence,
IV-bootstrap phases. Expiry-calendar / position-limit boot validation is
deferred to the expiry_guardian / strike_selector sub-projects, which are the
first consumers that actually need them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_BASE = Path(__file__).parent.parent.parent
_SETTINGS_PATH = _BASE / "config" / "options_settings.json"

_settings_cache: dict[str, Any] | None = None


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Return the ``options_trading`` block from options_settings.json (cached)."""
    global _settings_cache
    if path is None and _settings_cache is not None:
        return _settings_cache
    raw: dict[str, Any] = json.loads((path or _SETTINGS_PATH).read_text(encoding="utf-8"))
    block: dict[str, Any] = raw.get("options_trading", {})
    if path is None:
        _settings_cache = block
    return block


def is_enabled(settings: dict[str, Any] | None = None) -> bool:
    """True only if the master switch is explicitly on."""
    s = settings if settings is not None else load_settings()
    return bool(s.get("enabled", False))


def universe_indices(settings: dict[str, Any] | None = None) -> list[str]:
    s = settings if settings is not None else load_settings()
    indices: list[str] = s.get("universe", {}).get("indices", [])
    return indices


def chain_fetch_intervals(settings: dict[str, Any] | None = None) -> dict[str, int]:
    s = settings if settings is not None else load_settings()
    cfg = s.get("chain_fetch", {})
    return {
        "tier1": int(cfg.get("tier1_interval_seconds", 30)),
        "tier2": int(cfg.get("tier2_interval_seconds", 300)),
        "tier3": int(cfg.get("tier3_interval_seconds", 900)),
    }


def iv_bootstrap_phases(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    s = settings if settings is not None else load_settings()
    return dict(s.get("iv_bootstrap", {}))


def enabled_strategies(settings: dict[str, Any] | None = None) -> set[str]:
    """Strategy type names with ``enabled: true`` in the config's ``strategies``
    block. Note some of these (e.g. calendar_spread, protective_put) may not
    have a structure_builder implementation yet — the caller intersects this
    with what's actually buildable, rather than assuming config-enabled
    implies implemented."""
    s = settings if settings is not None else load_settings()
    strategies = s.get("strategies", {})
    return {
        name for name, cfg in strategies.items() if isinstance(cfg, dict) and cfg.get("enabled")
    }


def strike_deltas(settings: dict[str, Any] | None = None) -> dict[str, float]:
    """``strike_deltas`` block -- target deltas for structure_builder.py strike
    selection. Empty dict if not configured (callers fall back to their own
    spec-documented defaults)."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("strike_deltas", {}))


def dte_ranges(settings: dict[str, Any] | None = None) -> dict[str, list[int]]:
    """``dte_ranges`` block -- optimal (min_dte, max_dte) per strategy type for
    expiry_selector.py. Empty dict if not configured (callers fall back to
    their own spec-documented defaults)."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("dte_ranges", {}))


def expiry_rules(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``expiry_rules`` block (hard_exit_dte, min_entry_dte)."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("expiry_rules", {}))


def iv_thresholds(settings: dict[str, Any] | None = None) -> dict[str, float]:
    """``iv_thresholds`` block -- IVR floors read by strategy_selector.py's
    menu/reduced-size band. Empty dict if not configured (callers fall back
    to their own spec-documented defaults)."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("iv_thresholds", {}))


def low_ivr_mode(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``low_ivr_mode`` block -- opt-in tighter-delta/shorter-DTE profile for
    single-sided credit spreads below a configured IVR threshold. Off by
    default (enabled:false) -- see the block's own _note in
    options_settings.json for the 2026-09-04 backtest evidence behind the
    default values and why a directional credit-tilt was tested and
    rejected rather than offered here."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("low_ivr_mode", {}))


def direction_shadow(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``direction_shadow`` block -- research-only LLM expiry-direction forecast logged beside
    each credit spread (src/options/direction_shadow.py). Never gates a trade."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("direction_shadow", {}))


def pop_calibration(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``pop_calibration`` block -- empirically-anchored replacement for the EV
    gate's crude pop=1-delta touch-probability proxy, for bull_put_spread/
    bear_call_spread only (the two types scripts/backtest_options_seller_regime.py
    has actually validated). Off by default -- see the block's own _note in
    options_settings.json for the 2026-09-08 backtest evidence."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("pop_calibration", {}))


def ev_calibration(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``ev_calibration`` block -- per-structure-type theoretical win_rate/
    avg_win/avg_loss from scripts/backtest_options_seller_regime.py
    --all-structures (see the block's own _note for the exact backtest run
    this should be populated from). Off by default (enabled:false, and every
    per-type entry starts null) -- both this block's own "enabled" key AND
    config/learning.json's options_ev_calibration.mode must allow it before
    seller_desk.py/structure_selector.py substitute these numbers for the
    universal 0.5x/0.6x/1.5x heuristic multiplier."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("ev_calibration", {}))


def max_options_daily_loss_all_strategies_rupees() -> float:
    """CRM G1's segment-wide backstop: config/capital.json options.max_loss_per_day_all_options_strategies
    x live account equity (src/risk/budget.py) -- the single source of truth, replacing
    the old options_settings.json portfolio_risk.daily_loss_halt_pct x options_capital_rupees."""
    from src.risk.budget import get_budget, get_current_equity

    return get_budget().options.max_loss_per_day_all_strategies_rupees(get_current_equity())


def max_options_structure_loss_per_trade_rupees() -> float:
    """CRM G8's per-trade options risk cap: config/capital.json options.max_structure_loss_per_strategy
    x live account equity. Replaces operator.json's flat equity-sized cap (which silently
    shrank every real multi-lot options structure to 1 lot -- found 2026-10-06)."""
    from src.risk.budget import get_budget, get_current_equity

    return get_budget().options.max_structure_loss_per_strategy_rupees(get_current_equity())


def max_open_strategies() -> int:
    """Total open option STRATEGIES (an iron condor/straddle/spread = 1). Source of
    truth: config/capital.json options.max_open_strategies (src/risk/budget.py),
    2026-10-07 -- no longer read from options_settings.json."""
    from src.risk.budget import get_budget

    return get_budget().options.max_open_strategies


def max_strategies_per_index() -> int:
    """Open strategies allowed on one index at once. config/capital.json
    options.max_open_strategies_per_index."""
    from src.risk.budget import get_budget

    return get_budget().options.max_open_strategies_per_index


def max_same_direction_strategies() -> int:
    """Same-side directional spreads open at once across all indices. config/capital.json
    options.max_open_same_direction_spreads."""
    from src.risk.budget import get_budget

    return get_budget().options.max_open_same_direction_spreads


def adjustment_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``adjustments`` block -- delta_breach_threshold/blackout, cost_benefit_max_pct,
    max_adjustments_per_position, adjustment_block_below_dte, and the
    ``execute_rolls`` master switch (default off — see its own _note in
    options_settings.json). Empty dict if not configured (callers fall back
    to their own spec-documented defaults)."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("adjustments", {}))


def profit_and_stop(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``profit_and_stop`` block -- position_manager's profit-target/stop-loss/IV-crush
    percentages. Empty dict if not configured (position_manager falls back to
    the spec's own defaults)."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("profit_and_stop", {}))


def operator_close_position_ids(path: Path | None = None) -> list[str]:
    """``operator_actions.close_position_ids`` -- position ids the operator wants closed.

    Read from disk on EVERY call, bypassing the process-lifetime cache: an operator action
    must take effect on the next sweep without a restart. Any read/parse problem returns
    ``[]`` -- a bad edit to this file must never break the position sweep.
    """
    try:
        raw = json.loads((path or _SETTINGS_PATH).read_text(encoding="utf-8"))
        ids = (
            raw.get("options_trading", {}).get("operator_actions", {}).get("close_position_ids", [])
        )
    except (OSError, ValueError, AttributeError):
        return []
    return [i for i in ids if isinstance(i, str) and i] if isinstance(ids, list) else []


def margin_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``margin`` block -- margin_cushion_minimum_pct etc. Empty dict if not configured."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("margin", {}))


def portfolio_risk(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``portfolio_risk`` block -- daily_loss_halt_pct / gap_risk_enforce. Empty dict
    if not configured (SellerDesk falls back to the spec's 5% halt, gap enforce off)."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("portfolio_risk", {}))


def crm_adapter_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``crm_adapter`` block — placeholder values used by seller_crm_adapter.py
    to satisfy CRM's G1/G8 field guards for a credit-spread signal."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("crm_adapter", {}))


def weekly_condor(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``weekly_condor_simple`` block (hypothesis B). Empty dict = not configured,
    which SellerDesk.run_weekly_condor_cycle treats as disabled."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("weekly_condor_simple", {}))


def expiry_day(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``options_expiry_day`` block (hypothesis F2). Empty dict = not configured,
    which SellerDesk.run_expiry_day_cycle treats as disabled."""
    s = settings if settings is not None else load_settings()
    return dict(s.get("options_expiry_day", {}))


_short_premium_cache: tuple[float, dict[str, Any]] | None = None


def short_premium(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """``options_intraday_short_premium`` block. Empty dict = not configured = disabled.

    Unlike every other accessor here this one re-reads options_settings.json whenever the
    file's mtime changes, so entry/exit times, SL %, leg offsets etc. can be edited from
    the dashboard Configuration tab and apply on the next tick with no restart. An explicit
    ``settings`` argument (tests) bypasses the file entirely."""
    global _short_premium_cache
    if settings is not None:
        return dict(settings.get("options_intraday_short_premium", {}))
    try:
        mtime = _SETTINGS_PATH.stat().st_mtime
    except OSError:
        return dict(load_settings().get("options_intraday_short_premium", {}))
    if _short_premium_cache is None or _short_premium_cache[0] != mtime:
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        block = raw.get("options_trading", {}).get("options_intraday_short_premium", {})
        _short_premium_cache = (mtime, dict(block))
    return dict(_short_premium_cache[1])


def reload() -> None:
    """Clear the settings cache (re-read on next access)."""
    global _settings_cache, _short_premium_cache
    _settings_cache = None
    _short_premium_cache = None
