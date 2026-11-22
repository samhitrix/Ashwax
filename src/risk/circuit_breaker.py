"""Circuit breaker engine — encapsulates all trade-halt conditions for G2."""

from __future__ import annotations

import logging
from dataclasses import dataclass

# "src.risk.circuit_breaker" -> circuit_breaker.log (its own dedicated
# _COMPONENTS entry, distinct from "src.risk" -> risk.log). A BLOCK's reason
# already surfaces in gates.log via G2's GateResult, so this is a secondary
# trail, not the only record.
_log = logging.getLogger(__name__)


@dataclass
class BreakResult:
    tripped: bool
    reason: str = ""


class CircuitBreakerEngine:
    """Evaluates circuit-breaker conditions and returns a BreakResult.

    Called by CRM G2. Keeping this as a separate class allows the supervisor
    and other components to query breaker state without going through the full
    CRM pipeline.
    """

    def evaluate(self, consecutive_losses: int, max_consecutive_losses: int) -> BreakResult:
        """Return BreakResult(tripped=True) if any breaker condition is met."""
        if consecutive_losses >= max_consecutive_losses:
            result = BreakResult(
                tripped=True,
                reason=f"Circuit: {consecutive_losses} consecutive losses >= {max_consecutive_losses}",
            )
            _log.debug("circuit_breaker_found tripped=True reason=%s", result.reason)
            return result
        _log.debug(
            "circuit_breaker_found tripped=False consecutive_losses=%d max=%d",
            consecutive_losses,
            max_consecutive_losses,
        )
        return BreakResult(tripped=False)
