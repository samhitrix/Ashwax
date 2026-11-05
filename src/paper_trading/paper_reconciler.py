"""
PaperReconciler — reconcile in-memory paper positions vs DB paper_trades table.

No broker involved. Flags: ORPHAN_DB (DB open, not in memory) and
ORPHAN_MEMORY (in memory, not in DB).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.paper_trading.position_tracker import PaperPositionTracker

_LOG = logging.getLogger(__name__)  # "src.paper_trading.paper_reconciler" -> paper_trading.log


@dataclass
class PaperReconciliationGap:
    kind: str  # ORPHAN_DB | ORPHAN_MEMORY
    agent_trade_id: str
    symbol: str = ""


@dataclass
class PaperReconciliationResult:
    gaps: list[PaperReconciliationGap] = field(default_factory=list)
    clean: bool = True

    def __post_init__(self) -> None:
        self.clean = len(self.gaps) == 0


class PaperReconciler:
    """Reconciles in-memory tracker against DB paper_trades."""

    def __init__(self, tracker: PaperPositionTracker, pool: Any = None) -> None:
        self._tracker = tracker
        self._pool = pool

    async def reconcile(self, db_open_ids: list[str] | None = None) -> PaperReconciliationResult:
        if db_open_ids is None:
            db_open_ids = await self._fetch_db_open_ids()

        mem = {p.agent_trade_id for p in self._tracker.get_all()}
        db_set = set(db_open_ids)
        gaps: list[PaperReconciliationGap] = []

        for aid in db_set - mem:
            gaps.append(PaperReconciliationGap(kind="ORPHAN_DB", agent_trade_id=aid))

        for aid in mem - db_set:
            pos = self._tracker.get(aid)
            gaps.append(
                PaperReconciliationGap(
                    kind="ORPHAN_MEMORY", agent_trade_id=aid, symbol=pos.symbol if pos else ""
                )
            )

        return PaperReconciliationResult(gaps=gaps)

    async def _fetch_db_open_ids(self) -> list[str]:
        if self._pool is None:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT agent_trade_id FROM paper_trades "
                    "WHERE order_state NOT IN ('CLOSED','CANCELLED','REJECTED')"
                )
                return [r["agent_trade_id"] for r in rows]
        except Exception as exc:
            _LOG.error("paper_reconcile_db err=%s", exc)
            return []
