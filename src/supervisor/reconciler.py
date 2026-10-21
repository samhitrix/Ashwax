"""
Reconciler — broker vs DB position reconciliation.

Per docs/08-supervisor.md Boot Reconciliation Sequence:
  broker positions vs trades table open positions
  → ORPHANED_BROKER_POSITION: broker has it, DB doesn't
  → GHOST_DB_TRADE: DB has it, broker doesn't
  → QTY_MISMATCH: quantities differ
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from src.market.calendar import is_trading_day

if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.brokers.base import BrokerInterface

_IST = ZoneInfo("Asia/Kolkata")

_LOG = logging.getLogger(__name__)  # "src.supervisor.reconciler" -> supervisor.log


@dataclass
class ReconciliationGap:
    kind: str
    symbol: str
    broker_qty: int = 0
    db_qty: int = 0
    broker_avg_price: float = 0.0
    db_fill_price: float = 0.0
    agent_trade_id: str = ""
    product: str = ""  # canonical (DELIVERY | INTRADAY | MARGIN); set for orphans


@dataclass
class ReconciliationResult:
    gaps: list[ReconciliationGap] = field(default_factory=list)
    clean: bool = True

    def __post_init__(self) -> None:
        self.clean = len(self.gaps) == 0


_DB_PRODUCT_TO_CANONICAL: dict[str, str] = {
    "MIS": "INTRADAY",
    "CNC": "DELIVERY",
    "NRML": "MARGIN",
}


def _canonical_product(raw: str | None) -> str:
    """Normalize a product code to the same canonical form on both sides of the
    broker/DB boundary — the `trades` table stores raw Kite codes ("CNC"/"MIS"/
    "NRML"), while Position.product is already normalized to "DELIVERY"/
    "INTRADAY"/"MARGIN" by normalize_position(). Without this, a genuine CNC
    position (DB: "CNC") would never match its own broker Position (canonical:
    "DELIVERY") once matching keys on (symbol, product)."""
    if raw is None:
        return "DELIVERY"  # Holding carries no product field — holdings are always delivery.
    return _DB_PRODUCT_TO_CANONICAL.get(raw, raw)


def _effective_qty(pos: Any) -> int:
    """The real currently-held quantity for a broker_map entry (a Position or a
    Holding). A Holding's `t1_quantity` — bought the previous trading day(s),
    not yet moved into `quantity` by Zerodha's settlement — is real, currently-
    held stock, not a lesser state; every place that reads a broker_map entry's
    quantity must agree on this or a T1 holding just trades one false alarm
    (GHOST_DB_TRADE) for another (QTY_MISMATCH, comparing 0 against the DB's
    real quantity)."""
    if isinstance(pos, dict):
        return int(pos.get("quantity", 0) or 0)
    return int(getattr(pos, "quantity", 0) or 0) + int(getattr(pos, "t1_quantity", 0) or 0)


def _cnc_unsettled(ts: Any) -> bool:
    """True while a CNC entry placed at `ts` may legitimately be absent from both
    positions() and get_holdings(): from placement through the end of the next
    trading day. Kite only moves a delivery buy into holdings at T+1, and
    `entry_timestamp` is placement time, so an AMO placed on day E fills on the
    next trading day N and settles after that — a bare calendar-date test
    ("is it today?") expired at midnight and flagged a genuinely-held swing buy
    as a ghost (found 2026-09-22 00:03 IST: LAURUSLABS bought 14:56 the day
    before)."""
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    entry_day = ts.astimezone(_IST).date()
    nxt = entry_day + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return bool(datetime.now(_IST).date() <= nxt)


class Reconciler:
    """Compares broker positions with open trades in DB."""

    def __init__(
        self,
        broker: BrokerInterface,
        pool: Any = None,
        broker_name: str | None = None,
        known_brokers: Sequence[str] | None = None,
    ) -> None:
        """`broker_name` (multi-broker phase 2): reconcile only the live `trades` rows this
        broker holds (exec_broker; NULL means "kite") and key the orphan tracker by it. None =
        every live row, the legacy single-broker behaviour. `known_brokers` (the kite
        reconciler only) also gives it rows whose exec_broker names no registered broker --
        the same ownership rule as the order-monitor reseed."""
        self._broker = broker
        self._pool = pool
        self._broker_name = broker_name
        self._known_brokers = list(known_brokers) if known_brokers else []

    async def reconcile(
        self, db_open_trades: dict[str, list[dict[str, Any]]] | None = None
    ) -> ReconciliationResult:
        """
        Reconcile broker positions against DB open trades.

        db_open_trades: {symbol: [{qty, fill_price, agent_trade_id, product,
        entry_timestamp}, ...]} — pass in if no pool. A symbol maps to a LIST
        because more than one trade can legitimately be open for it at once —
        a CNC swing hold and an MIS intraday trade on the same symbol are two
        independent broker-side positions, and even two trades of the SAME
        product can both be "open" in DB terms (found 2026-09-08: CGCL had a
        FILLED CNC trade and a still-SUBMITTED duplicate both non-terminal at
        once). The single-dict-per-symbol shape this used to have silently
        dropped whichever row Postgres happened to return last for a symbol —
        an invisible gap in reconciliation coverage, not just a display bug.

        Matching is keyed on (symbol, product) — see _canonical_product for why
        both sides need normalizing to the same vocabulary first.
        """
        gaps: list[ReconciliationGap] = []

        try:
            raw = await self._broker.get_positions()
            # get_positions() returns list[Position] per BrokerInterface (kite/paper
            # adapters) — the `raw.get("net", [])` dict-shaped path below was dead
            # code against that contract (isinstance(raw, dict) is always False),
            # so broker_map was silently empty and EVERY DB-open trade reported as
            # GHOST_DB_TRADE regardless of whether the broker actually held it
            # (found 2026-09-02 investigating a real IFCI/JINDALSAW gap report).
            net: list[Any] = (
                raw.get("net", []) if isinstance(raw, dict) else list(raw or [])  # type: ignore[attr-defined]
            )
            broker_map: dict[tuple[str, str], Any] = {}
            for p in net:
                sym = getattr(p, "symbol", None) or (
                    p.get("tradingsymbol") if isinstance(p, dict) else None
                )
                qty = getattr(p, "quantity", None)
                if qty is None and isinstance(p, dict):
                    qty = p.get("quantity")
                product = getattr(p, "product", None) or (
                    p.get("product") if isinstance(p, dict) else None
                )
                # DELIVERY (CNC) entries in positions() are same-day-transaction
                # rows, not proof of a holding — normalize_position() abs()'s the
                # quantity, so a same-day SELL of an entire holding (raw qty
                # negative) looks identical to a genuine buy once abs'd, and was
                # wrongly counted as "still held" (found 2026-09-02: this hid the
                # exact IFCI ghost trade the CRITICAL-vs-CORRECTED fix above exists
                # to catch). Holdings-only for CNC — positions() stays authoritative
                # for MIS/NRML, which have no separate holdings concept.
                if sym and qty and product != "DELIVERY":
                    broker_map[(sym, _canonical_product(product))] = p
            # CNC/delivery holdings with no same-day transaction don't appear in
            # get_positions() (Kite's day position book) at all — only in
            # get_holdings(). Without this, every untouched multi-day swing
            # position would also be misreported as a ghost trade.
            try:
                holdings = await self._broker.get_holdings()
            except Exception:
                holdings = []
            for h in holdings:
                sym = getattr(h, "symbol", None)
                if not sym:
                    continue
                key = (sym, "DELIVERY")
                if _effective_qty(h) and key not in broker_map:
                    broker_map[key] = h
        except Exception as exc:
            _LOG.error("reconcile_broker_fetch_failed err=%s", exc)
            return ReconciliationResult()

        if db_open_trades is None:
            db_open_trades = await self._fetch_db_open_trades()

        db_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for symbol, trades in db_open_trades.items():
            for trade in trades:
                db_by_key[(symbol, _canonical_product(trade.get("product")))] = trade

        for (symbol, _product), pos in broker_map.items():
            qty = _effective_qty(pos)
            if qty != 0 and (symbol, _product) not in db_by_key:
                avg = (
                    getattr(pos, "average_price", 0.0)
                    if not isinstance(pos, dict)
                    else pos.get("average_price", 0.0)
                )
                gaps.append(
                    ReconciliationGap(
                        kind="ORPHANED_BROKER_POSITION",
                        symbol=symbol,
                        broker_qty=int(qty),
                        broker_avg_price=avg,
                        product=_product,
                    )
                )

        for (symbol, product), trade in db_by_key.items():
            key = (symbol, product)
            if key not in broker_map:
                # A CNC position bought TODAY is invisible to both halves of broker_map:
                # excluded from positions() by the DELIVERY filter above (same-day rows
                # there can't be trusted as still-held vs a completed round-trip — see
                # that comment), and not yet in get_holdings() either, since Kite only
                # settles a delivery buy into holdings at T+1. There is no way to
                # confirm same-day CNC either way right now, so don't raise a false
                # "ghost" alarm for it — _auto_close_ghost_trades' own broker-order-book
                # check is still the real safety net if this needs closing later, and it
                # already refuses to close without independent proof either way (found
                # 2026-09-07: CGCL, filled same-day, flagged GHOST every reconcile cycle
                # despite genuinely being held — confirmed directly against the broker).
                if product == "DELIVERY" and _cnc_unsettled(trade.get("entry_timestamp")):
                    continue
                gaps.append(
                    ReconciliationGap(
                        kind="GHOST_DB_TRADE",
                        symbol=symbol,
                        db_qty=trade.get("qty", 0),
                        agent_trade_id=trade.get("agent_trade_id", ""),
                    )
                )
            else:
                pos = broker_map[key]
                b_qty = _effective_qty(pos)
                d_qty = trade.get("qty", 0)
                if b_qty != d_qty:
                    b_avg = (
                        getattr(pos, "average_price", 0.0)
                        if not isinstance(pos, dict)
                        else pos.get("average_price", 0.0)
                    )
                    gaps.append(
                        ReconciliationGap(
                            kind="QTY_MISMATCH",
                            symbol=symbol,
                            broker_qty=b_qty,
                            db_qty=d_qty,
                            broker_avg_price=b_avg,
                            db_fill_price=trade.get("fill_price", 0.0),
                            agent_trade_id=trade.get("agent_trade_id", ""),
                        )
                    )

        gaps = await self._track_external_positions(gaps)
        if gaps:
            _LOG.warning("reconciliation_gaps count=%d", len(gaps))
        return ReconciliationResult(gaps=gaps)

    async def _track_external_positions(
        self, gaps: list[ReconciliationGap]
    ) -> list[ReconciliationGap]:
        """Record ORPHANED_BROKER_POSITION gaps in external_positions (migration 074) and
        drop the ones a human has acknowledged. First sight / disappearance is logged
        once instead of on every cycle. Fail-safe: any error returns ``gaps``
        unchanged so a tracking fault can never hide a real orphan."""
        if self._pool is None:
            return gaps
        try:
            from src.db.repositories.external_positions import ExternalPositionRepository

            orphans = [
                (g.symbol, g.product, g.broker_qty, g.broker_avg_price)
                for g in gaps
                if g.kind == "ORPHANED_BROKER_POSITION"
            ]
            _repo = ExternalPositionRepository(self._pool)
            sync = (
                await _repo.sync(orphans)
                if self._broker_name is None
                else await _repo.sync(orphans, exec_broker=self._broker_name)
            )
        except Exception as exc:
            _LOG.warning("external_positions_sync_failed err=%s -- reporting all gaps", exc)
            return gaps
        for symbol, product in sync.new:
            _LOG.warning(
                "external_position_detected symbol=%s product=%s -- not opened by this agent; "
                "recorded in external_positions (acknowledge via POST "
                "/api/external_positions/{id}/ack to stop reporting)",
                symbol,
                product,
            )
        for symbol, product in sync.closed:
            _LOG.info("external_position_closed symbol=%s product=%s", symbol, product)
        return [
            g
            for g in gaps
            if not (
                g.kind == "ORPHANED_BROKER_POSITION" and (g.symbol, g.product) in sync.acknowledged
            )
        ]

    async def _fetch_db_open_trades(self) -> dict[str, list[dict[str, Any]]]:
        """Only FILLED trades represent real broker-side exposure worth
        reconciling here — a SUBMITTED/OPEN (still-resting, never-filled) order
        has no position at the broker yet by definition, so it's out of scope
        for this check (order_lifecycle.py's repeg/monitor loop owns that). This
        used to select any non-terminal state, which meant a still-resting
        duplicate order (found 2026-09-08: CGCL's SUBMITTED oneil_canslim row)
        could arbitrarily win the old symbol-keyed dict over the real FILLED
        trade, with no ORDER BY to make that deterministic either way."""
        if self._pool is None:
            return {}
        try:
            async with self._pool.acquire() as conn:
                rows = await self._fetch_open_rows(conn)
                grouped: dict[str, list[dict[str, Any]]] = {}
                for r in rows:
                    grouped.setdefault(r["symbol"], []).append(
                        {
                            "qty": r["quantity"],
                            "fill_price": float(r["fill_price"] or 0),
                            "agent_trade_id": r["agent_trade_id"],
                            "product": r["product"],
                            "entry_timestamp": r["entry_timestamp"],
                        }
                    )
                return grouped
        except Exception as exc:
            _LOG.error("reconcile_db_fetch err=%s", exc)
            return {}

    async def _fetch_open_rows(self, conn: Any) -> list[Any]:
        sql = """
                    SELECT symbol, quantity, fill_price, agent_trade_id, product, entry_timestamp
                    FROM trades
                    WHERE order_state = 'FILLED'
                    """
        if self._broker_name is None:
            return list(await conn.fetch(sql))
        args: list[Any] = [self._broker_name]
        own = "COALESCE(exec_broker, 'kite') = $1"
        if self._broker_name == "kite" and self._known_brokers:
            args.append(self._known_brokers)
            own = f"({own} OR COALESCE(exec_broker, 'kite') <> ALL($2::text[]))"
        try:
            return list(await conn.fetch(f"{sql} AND {own}", *args))
        except Exception as exc:
            if "exec_broker" not in str(exc) or "does not exist" not in str(exc):
                raise
            # Pre-089 database: every live row predates routing (placed through Kite).
            _LOG.warning(
                "reconcile_exec_broker_missing broker=%s -- migration 089 not applied",
                self._broker_name,
            )
            return list(await conn.fetch(sql)) if self._broker_name == "kite" else []
