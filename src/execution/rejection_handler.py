"""
RejectionHandler — auto-classify and retry rejected orders.

Per docs/05-order-lifecycle.md:
  MARGIN insufficient    → reduce qty, retry (max 2)
  Product type error     → switch MIS↔CNC, retry once
  Circuit filter         → 30-min blackout for symbol
  Token invalid          → TOTP refresh, retry
  Unknown               → log, raise incident, no retry

Max auto-retries: 2 for any rejection before escalating.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")
_log = logging.getLogger(__name__)  # nests under "src.execution" -> execution.log


class RejectionType(StrEnum):
    MARGIN_INSUFFICIENT = "MARGIN_INSUFFICIENT"
    PRODUCT_TYPE_ERROR = "PRODUCT_TYPE_ERROR"
    CIRCUIT_FILTER = "CIRCUIT_FILTER"
    TOKEN_INVALID = "TOKEN_INVALID"  # noqa: S105  # noqa: S105
    UNKNOWN = "UNKNOWN"


@dataclass
class RejectionResult:
    rejection_type: RejectionType
    should_retry: bool
    suggested_quantity: int | None
    suggested_product: str | None
    retry_after_seconds: int = 0
    symbol_blackout_until: datetime | None = None
    incident_required: bool = False


_REJECTION_PATTERNS: list[tuple[list[str], RejectionType]] = [
    (
        ["insufficient margin", "margin shortage", "not enough margin"],
        RejectionType.MARGIN_INSUFFICIENT,
    ),
    (
        ["product type", "invalid product", "mis not allowed", "cnc not allowed"],
        RejectionType.PRODUCT_TYPE_ERROR,
    ),
    (["circuit", "price band", "upper circuit", "lower circuit"], RejectionType.CIRCUIT_FILTER),
    (
        ["invalid token", "session expired", "access token", "unauthorized"],
        RejectionType.TOKEN_INVALID,
    ),
]


class RejectionHandler:
    """
    Classifies order rejections and determines retry strategy.

    Tracks retry counts per agent_trade_id.
    max_retries and retry_delay_seconds are loaded from execution.json rejection_retry.
    """

    CIRCUIT_BLACKOUT_MINUTES = 30

    def __init__(self, max_retries: int = 2, retry_delay_seconds: int = 1) -> None:
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds
        self._retry_counts: dict[str, int] = {}
        self._symbol_blackouts: dict[str, datetime] = {}

    def classify(
        self,
        agent_trade_id: str,
        rejection_reason: str,
        current_quantity: int,
        current_product: str,
    ) -> RejectionResult:
        """Classify rejection and return retry strategy."""
        _log.debug(
            "rejection_classify_received tid=%s reason=%s qty=%d product=%s",
            agent_trade_id,
            rejection_reason,
            current_quantity,
            current_product,
        )
        rejection_type = self._detect_type(rejection_reason.lower())
        retries_so_far = self._retry_counts.get(agent_trade_id, 0)
        _log.debug(
            "rejection_classify_found tid=%s type=%s retries_so_far=%d max_retries=%d",
            agent_trade_id,
            rejection_type.value,
            retries_so_far,
            self._max_retries,
        )

        if retries_so_far >= self._max_retries:
            _log.debug("rejection_classify_action tid=%s action=escalate_incident", agent_trade_id)
            return RejectionResult(
                rejection_type=rejection_type,
                should_retry=False,
                suggested_quantity=None,
                suggested_product=None,
                incident_required=True,
            )

        self._retry_counts[agent_trade_id] = retries_so_far + 1

        if rejection_type == RejectionType.MARGIN_INSUFFICIENT:
            reduced_qty = max(1, int(current_quantity * 0.75))
            _log.debug(
                "rejection_classify_action tid=%s action=retry_reduced_qty %d->%d",
                agent_trade_id,
                current_quantity,
                reduced_qty,
            )
            return RejectionResult(
                rejection_type=rejection_type,
                should_retry=True,
                suggested_quantity=reduced_qty,
                suggested_product=None,
            )

        if rejection_type == RejectionType.PRODUCT_TYPE_ERROR:
            switched = "INTRADAY" if current_product == "DELIVERY" else "DELIVERY"
            _log.debug(
                "rejection_classify_action tid=%s action=retry_switch_product %s->%s "
                "should_retry=%s",
                agent_trade_id,
                current_product,
                switched,
                retries_so_far < 1,
            )
            return RejectionResult(
                rejection_type=rejection_type,
                should_retry=retries_so_far < 1,
                suggested_quantity=current_quantity,
                suggested_product=switched,
            )

        if rejection_type == RejectionType.CIRCUIT_FILTER:
            blackout_until = datetime.now(_IST) + timedelta(minutes=self.CIRCUIT_BLACKOUT_MINUTES)
            _log.debug(
                "rejection_classify_action tid=%s action=blackout_until=%s",
                agent_trade_id,
                blackout_until.isoformat(),
            )
            return RejectionResult(
                rejection_type=rejection_type,
                should_retry=False,
                suggested_quantity=None,
                suggested_product=None,
                symbol_blackout_until=blackout_until,
            )

        if rejection_type == RejectionType.TOKEN_INVALID:
            _log.debug("rejection_classify_action tid=%s action=retry_after_60s", agent_trade_id)
            return RejectionResult(
                rejection_type=rejection_type,
                should_retry=True,
                suggested_quantity=current_quantity,
                suggested_product=None,
                retry_after_seconds=60,
            )

        _log.debug("rejection_classify_action tid=%s action=unknown_no_retry", agent_trade_id)
        return RejectionResult(
            rejection_type=RejectionType.UNKNOWN,
            should_retry=False,
            suggested_quantity=None,
            suggested_product=None,
            incident_required=True,
        )

    def is_symbol_blacked_out(self, symbol: str) -> bool:
        until = self._symbol_blackouts.get(symbol)
        if until is None:
            return False
        if datetime.now(_IST) < until:
            return True
        del self._symbol_blackouts[symbol]
        return False

    def set_blackout(self, symbol: str, until: datetime) -> None:
        self._symbol_blackouts[symbol] = until

    def clear_retries(self, agent_trade_id: str) -> None:
        self._retry_counts.pop(agent_trade_id, None)

    @staticmethod
    def _detect_type(reason_lower: str) -> RejectionType:
        for keywords, rtype in _REJECTION_PATTERNS:
            if any(kw in reason_lower for kw in keywords):
                return rtype
        return RejectionType.UNKNOWN
