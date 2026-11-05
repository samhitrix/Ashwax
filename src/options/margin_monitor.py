"""Seller-desk margin monitor (options_spec.md §11).

Absent from the original master spec — added specifically to prevent
broker-forced liquidation, which closes at the worst price with no control
over leg order. Voluntarily exiting at 60% margin expansion beats being
force-closed at 100%.
"""

from __future__ import annotations

from dataclasses import dataclass

_WARNING_PCT = 30.0
_CRITICAL_PCT = 60.0
_BLOCK_CUSHION_PCT = 20.0
_RESUME_CUSHION_PCT = 30.0


@dataclass
class MarginAlert:
    level: str  # "OK" | "WARNING" | "CRITICAL"
    expansion_pct: float
    reason: str


def position_margin_expansion(entry_margin: float, current_margin: float) -> float | None:
    if entry_margin <= 0.0:
        return None
    return (current_margin - entry_margin) / entry_margin * 100.0


def evaluate_position_margin(entry_margin: float, current_margin: float) -> MarginAlert:
    expansion = position_margin_expansion(entry_margin, current_margin)
    if expansion is None:
        return MarginAlert("OK", 0.0, "entry_margin unavailable")
    if expansion > _CRITICAL_PCT:
        return MarginAlert(
            "CRITICAL",
            expansion,
            f"margin expanded {expansion:.0f}% — close now, before broker force-close",
        )
    if expansion > _WARNING_PCT:
        return MarginAlert(
            "WARNING", expansion, f"margin expanded {expansion:.0f}% — consider early exit"
        )
    return MarginAlert("OK", expansion, "within normal range")


def cushion_pct(available_margin: float, total_used_margin: float) -> float | None:
    if total_used_margin <= 0.0:
        return None
    return available_margin / total_used_margin * 100.0


def new_entries_blocked(
    available_margin: float,
    total_used_margin: float,
    *,
    currently_blocked: bool,
    block_cushion_pct: float = _BLOCK_CUSHION_PCT,
) -> tuple[bool, str]:
    """Hysteresis: block below ``block_cushion_pct`` (default 20%), only resume
    once the cushion recovers by the spec's own 10-point gap (20% -> 30%) —
    avoids flapping the block on and off around a single threshold."""
    resume_cushion_pct = block_cushion_pct + (_RESUME_CUSHION_PCT - _BLOCK_CUSHION_PCT)
    cushion = cushion_pct(available_margin, total_used_margin)
    if cushion is None:
        return currently_blocked, "total_used_margin unavailable"
    if cushion < block_cushion_pct:
        return True, f"cushion {cushion:.0f}% < block threshold {block_cushion_pct:.0f}%"
    if currently_blocked and cushion < resume_cushion_pct:
        return True, f"cushion {cushion:.0f}% below resume threshold {resume_cushion_pct:.0f}%"
    return False, f"cushion {cushion:.0f}% OK"
