"""Shared EV-calibration gating for both options decision pipelines —
src/options/seller_desk.py (regime/IVR-driven, 8 structure types) and
src/options/structure_selector.py (equity-signal-driven: bull_call_spread/
long_call/protective_put). One config block (config/options_settings.json
ev_calibration, src/options/seller_settings.py::ev_calibration()) and one
config/learning.json mode key (options_ev_calibration.mode) drive both — the
underlying payoff math for a shared structure_type (e.g. bull_call_spread) is
identical regardless of which engine built it, so the calibration data must
not fork between them.

Three-state mode, same convention as every other learning-engine gate: off =
today's universal 0.5x/0.6x/1.5x heuristic multiplier, unchanged. shadow =
the calibrated EV is also computed (for the caller to log) but never used for
the actual decision. live = the calibrated avg_win/avg_loss substitute the
heuristic wherever config/options_settings.json ev_calibration has a
populated, enabled entry for that structure_type; an absent/malformed entry
always falls back to the heuristic, never a hard failure — matches
strategy_selector.calibrated_pop()'s own fallback-on-malformed-config pattern.
"""

from __future__ import annotations

from typing import Any, cast

from src.options import seller_settings as _seller_settings


def ev_calibration_mode() -> str:
    """config/learning.json options_ev_calibration.mode, or "off" if the file
    or key is missing/unreadable."""
    try:
        import json as _json
        from pathlib import Path as _Path

        cfg_path = _Path("config/learning.json")
        if not cfg_path.exists():
            return "off"
        cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
        return str(cfg.get("options_ev_calibration", {}).get("mode") or "off")
    except Exception:
        return "off"


def _heuristic_ev_inputs(
    direction: str, net_premium_1lot: float, max_profit_1lot: float, max_loss_1lot: float
) -> tuple[float, float]:
    """Today's live formula, unchanged — the fallback for every mode and every
    structure_type without a populated calibration entry."""
    avg_win = (0.5 * net_premium_1lot) if direction == "credit" else (0.6 * max_profit_1lot)
    avg_loss = (1.5 * net_premium_1lot) if direction == "credit" else max_loss_1lot
    return avg_win, avg_loss


def calibrated_ev_inputs(
    structure_type: str,
    direction: str,
    net_premium_1lot: float,
    max_profit_1lot: float,
    max_loss_1lot: float,
) -> tuple[float, float, dict[str, float] | None]:
    """Returns (avg_win, avg_loss, calibrated_shadow) where calibrated_shadow
    is the calibrated (avg_win, avg_loss) as a dict IF a populated entry
    exists for structure_type — regardless of mode, so callers can log a
    shadow comparison even in "off" mode's absence... actually: None when no
    entry exists at all, populated whenever calibration data is available,
    letting the caller decide (via ev_calibration_mode()) whether to act on it
    or just log it.

    The returned (avg_win, avg_loss) tuple is the ACTUAL value to use for the
    EV gate: the heuristic in "off"/"shadow" mode, or in every case where no
    calibration entry exists; the calibrated value only in "live" mode with a
    populated entry.
    """
    heuristic_win, heuristic_loss = _heuristic_ev_inputs(
        direction, net_premium_1lot, max_profit_1lot, max_loss_1lot
    )
    entry = _calibration_entry(structure_type)
    if entry is None:
        return heuristic_win, heuristic_loss, None

    # avg_win/avg_loss are stored as multiples of premium/max_loss (position-
    # size-independent) — see config/options_settings.json ev_calibration._note.
    calibrated_win = float(entry.get("avg_win", 0.0)) * (
        net_premium_1lot if direction == "credit" else max_profit_1lot
    )
    calibrated_loss = float(entry.get("avg_loss", 0.0)) * (
        net_premium_1lot if direction == "credit" else max_loss_1lot
    )
    calibrated_shadow = {"avg_win": calibrated_win, "avg_loss": calibrated_loss}

    if ev_calibration_mode() == "live":
        return calibrated_win, calibrated_loss, calibrated_shadow
    return heuristic_win, heuristic_loss, calibrated_shadow


def calibrated_win_rate(structure_type: str) -> float | None:
    """win_rate_pct/100 from a populated, enabled ev_calibration entry for
    structure_type — None if no such entry exists. Used by
    structure_selector.py's _p_win() to source its win-probability estimate
    from real backtested data instead of the raw conviction clamp, when
    options_ev_calibration.mode == "live" (structure_selector.py checks the
    mode itself, same as calibrated_ev_inputs's callers)."""
    entry = _calibration_entry(structure_type)
    if entry is None or "win_rate_pct" not in entry:
        return None
    try:
        return float(entry["win_rate_pct"]) / 100.0
    except (TypeError, ValueError):
        return None


def _calibration_entry(structure_type: str) -> dict[str, Any] | None:
    try:
        block = _seller_settings.ev_calibration()
        if not block.get("enabled", False):
            return None
        entry = block.get(structure_type)
        if not entry or not isinstance(entry, dict):
            return None
        if "avg_win" not in entry or "avg_loss" not in entry:
            return None
        return cast(dict[str, Any], entry)
    except Exception:
        return None
