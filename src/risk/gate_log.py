"""Per-gate CRM decision trail -> logs/gates.log.

Before 2026-09-11 there was no way to see why a trade passed. `crm.evaluate()`
returns the full G0-G10 verdict list and the API persists it, but the only thing
ever LOGGED was the first blocking gate (`swing_crm_blocked gate=G4`); an
APPROVED signal produced no gate detail at all. Reviewing a live trade meant
reconstructing the gate sequence by hand.

This module writes one line per gate plus a FINAL line, for EVERY signal that
reaches CRM — every strategy, every segment, approved or blocked. It is
observability only: `log_gate_decisions` never raises, because a logging failure
must never be able to kill a trade path.

Format is pipe-delimited to match the rest of this project's logs (not JSON), so
it greps cleanly:

    grep '| SWING |'  logs/gates.log     # one segment
    grep '| G9 |'     logs/gates.log     # one gate across all segments
    grep 'FINAL | BLOCKED' logs/gates.log
"""

from __future__ import annotations

from typing import Any

from src.infrastructure.logger import get_logger
from src.risk.crm import segment_of

_log = get_logger("gates")

# Gate/Purpose columns from docs/crm_gates.md — kept in sync with that table so a
# bare "G7" in the log (meaningless without opening the doc) reads as something a
# trader can act on without cross-referencing anything.
_GATE_LABELS: dict[str, tuple[str, str]] = {
    "G0": ("Master switch", "Global kill-switch"),
    "G1": ("Hard caps", "Absolute risk floors"),
    "G2": ("Circuit breakers", "System-wide halts"),
    "G3": ("Time-of-day", "IST trading windows"),
    "G4": ("Regime", "Market/sector context"),
    "G5": ("News", "Event risk"),
    "G6": ("Strategy health", "Live performance"),
    "G7": ("Portfolio/margin", "Exposure limits"),
    "G8": ("SL sizing", "Position sizing"),
    "G9": ("LLM confirmation", "LLM sanity check"),
    "G10": ("Autonomy mode", "Human-in-the-loop"),
}


def _gate_label(gate_id: str) -> str:
    """`"G7"` -> `"G7-Portfolio/margin"`; unknown ids pass through unchanged."""
    name = _GATE_LABELS.get(gate_id)
    return f"{gate_id}-{name[0]}" if name else gate_id


def _gate_purpose(gate_id: str) -> str:
    name = _GATE_LABELS.get(gate_id)
    return name[1] if name else "-"


def log_gate_decisions(signal: Any, decision: Any) -> None:
    """Emit the full gate trail for one CRM decision.

    Args:
        signal: the CRMSignal that was evaluated.
        decision: the CRMDecision returned by `crm.evaluate()`.

    Never raises — malformed input is swallowed rather than propagated into the
    trade path.
    """
    try:
        segment = str(segment_of(signal)).upper()
        strategy = str(getattr(signal, "strategy_name", "") or "-")
        symbol = str(getattr(signal, "symbol", "") or "-")
        head = f"GATE | {segment} | {strategy} | {symbol}"

        for gate in getattr(decision, "gate_results", None) or []:
            reason = str(getattr(gate, "reason", "") or "-")
            qty = getattr(gate, "modified_quantity", None)
            # Only MODIFY gates carry a quantity; showing it is the whole point of
            # the line (G8 re-sizing is otherwise invisible).
            qty_part = f" | qty={qty}" if qty is not None else ""
            _log.info(
                f"{head} | {_gate_label(gate.gate_id)} | {_gate_purpose(gate.gate_id)} "
                f"| {gate.decision.value} | {reason}{qty_part}"
            )

        if getattr(decision, "approved", False):
            _log.info(f"{head} | FINAL | APPROVED | qty={getattr(decision, 'final_quantity', 0)}")
        else:
            blocking = str(getattr(decision, "blocking_gate", "") or "-")
            why = str(getattr(decision, "blocking_reason", "") or "-")
            _log.info(f"{head} | FINAL | BLOCKED | gate={_gate_label(blocking)} | {why}")
    except Exception as exc:  # pragma: no cover - defensive, see module docstring
        try:
            _log.warning(f"gate_log_failed | err={exc}")
        except Exception:
            pass
