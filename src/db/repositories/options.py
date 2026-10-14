"""OptionsRepository — options_structures + options_iv_history tables (Phase 6).

Also covers the seller-desk vol-intelligence tables (options_vol_intel_history,
options_vol_surface_snapshots) written by src/options/volatility_engine.py.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

    from src.options.greeks_budget import PortfolioGreeks
    from src.options.options_executor import StructureResult
    from src.options.structure_selector import OptionLeg, StructureChoice


def _serialize_leg(leg: OptionLeg) -> dict[str, object]:
    return {
        "tradingsymbol": leg.tradingsymbol,
        "instrument_type": leg.instrument_type,
        "strike": leg.strike,
        "direction": leg.direction,
        "lots": leg.lots,
        "lot_size": leg.lot_size,
        "premium": leg.premium,
        "delta": leg.delta,
    }


class OptionsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ── structures ───────────────────────────────────────────────────────────

    async def insert_structure(
        self,
        structure_id: str,
        signal_id: str,
        choice: StructureChoice,
        result: StructureResult,
        is_paper: bool = True,
    ) -> None:
        legs = [
            {
                "tradingsymbol": leg.tradingsymbol,
                "type": leg.instrument_type,
                "strike": leg.strike,
                "direction": leg.direction,
                "lots": leg.lots,
                "lot_size": leg.lot_size,
                "premium": leg.premium,
                "delta": leg.delta,
            }
            for leg in choice.legs
        ]
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO options_structures (
                    structure_id, signal_id, symbol, structure_type, product,
                    legs, premium_paid, max_loss, max_profit, rr, expected_value,
                    net_delta, leg_order_ids, sl_order_id, reasoning, is_paper,
                    target, entry_conviction
                ) VALUES (
                    $1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12,
                    $13::jsonb, $14, $15, $16, $17, $18
                )
                ON CONFLICT (structure_id) DO NOTHING
                """,
                structure_id,
                signal_id,
                choice.symbol,
                choice.structure_type,
                choice.product,
                json.dumps(legs),
                float(choice.max_loss),
                float(choice.max_loss),
                float(choice.max_profit),
                float(choice.rr),
                float(choice.expected_value),
                float(choice.net_delta),
                json.dumps(result.leg_order_ids),
                result.sl_order_id,
                choice.reasoning,
                is_paper,
                float(choice.target),
                float(choice.confidence),
            )

    async def close_structure(
        self,
        structure_id: str,
        *,
        exit_reason: str,
        realized_pnl: float | None,
        status: str = "CLOSED",
    ) -> None:
        """Mark a buyer-first options_structures row closed with a real
        outcome — Options learning-engine PR5. Previously there was no code
        path that ever transitioned a row away from OPEN with an outcome;
        rows stayed OPEN forever regardless of what actually happened."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE options_structures
                   SET status = $2, closed_at = NOW(), realized_pnl = $3, exit_reason = $4
                 WHERE structure_id = $1
                """,
                structure_id,
                status,
                realized_pnl,
                exit_reason,
            )

    async def load_open_structures(self, is_paper: bool | None = None) -> list[asyncpg.Record]:
        """OPEN options_structures rows for structure_monitor.py's exit sweep."""
        async with self._pool.acquire() as conn:
            if is_paper is None:
                return list(
                    await conn.fetch("SELECT * FROM options_structures WHERE status = 'OPEN'")
                )
            return list(
                await conn.fetch(
                    "SELECT * FROM options_structures WHERE status = 'OPEN' AND is_paper = $1",
                    is_paper,
                )
            )

    async def load_closed_structures_with_outcome(
        self, structure_type: str | None = None, is_paper: bool | None = None
    ) -> list[asyncpg.Record]:
        """CLOSED options_structures rows with a real realized_pnl — the data
        source for Options learning-engine PR6's conviction calibration.

        is_paper: None = both (today's default — options trades paper-only,
        so this is currently a no-op distinction, but the filter exists so
        conviction calibration correctly separates paper vs live the moment
        options ever goes live, rather than silently mixing them the way
        _calibration_loop/_learning_loop did before being fixed for the same
        reason on the equity side)."""
        async with self._pool.acquire() as conn:
            if structure_type is None and is_paper is None:
                return list(
                    await conn.fetch(
                        "SELECT * FROM options_structures "
                        "WHERE status = 'CLOSED' AND realized_pnl IS NOT NULL "
                        "ORDER BY closed_at ASC"
                    )
                )
            if structure_type is not None and is_paper is None:
                return list(
                    await conn.fetch(
                        "SELECT * FROM options_structures "
                        "WHERE status = 'CLOSED' AND realized_pnl IS NOT NULL AND structure_type = $1 "
                        "ORDER BY closed_at ASC",
                        structure_type,
                    )
                )
            if structure_type is None:
                return list(
                    await conn.fetch(
                        "SELECT * FROM options_structures "
                        "WHERE status = 'CLOSED' AND realized_pnl IS NOT NULL AND is_paper = $1 "
                        "ORDER BY closed_at ASC",
                        is_paper,
                    )
                )
            return list(
                await conn.fetch(
                    "SELECT * FROM options_structures "
                    "WHERE status = 'CLOSED' AND realized_pnl IS NOT NULL AND structure_type = $1 "
                    "AND is_paper = $2 ORDER BY closed_at ASC",
                    structure_type,
                    is_paper,
                )
            )

    async def upsert_conviction_calibration(
        self,
        structure_type: str,
        p_win_floor: float,
        p_win_ceiling: float,
        sample_count: int,
        brier_score: float | None,
        worst_gap: float | None,
        is_paper: bool = True,
    ) -> None:
        """Persist a Layer-6-style conviction-calibration adjustment (Options
        learning-engine PR6) — makes structure_selector.py's _p_win() clamp
        bounds restart-safe, mirroring equity's calibration_thresholds
        persistence pattern but for a per-(structure_type, is_paper) bound
        pair — paper and live are tracked independently (is_paper defaults to
        True since options trades paper-only today)."""
        from decimal import Decimal as _D

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO options_conviction_calibration
                    (structure_type, is_paper, p_win_floor, p_win_ceiling, sample_count,
                     brier_score, worst_gap, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                ON CONFLICT (structure_type, is_paper) DO UPDATE
                  SET p_win_floor   = EXCLUDED.p_win_floor,
                      p_win_ceiling = EXCLUDED.p_win_ceiling,
                      sample_count  = EXCLUDED.sample_count,
                      brier_score   = EXCLUDED.brier_score,
                      worst_gap     = EXCLUDED.worst_gap,
                      updated_at    = NOW()
                """,
                structure_type,
                is_paper,
                _D(str(round(p_win_floor, 4))),
                _D(str(round(p_win_ceiling, 4))),
                sample_count,
                _D(str(round(brier_score, 4))) if brier_score is not None else None,
                _D(str(round(worst_gap, 4))) if worst_gap is not None else None,
            )

    async def load_conviction_calibration(
        self, is_paper: bool = True
    ) -> dict[str, tuple[float, float]]:
        """{structure_type: (p_win_floor, p_win_ceiling)} for every persisted
        entry matching `is_paper` — loaded once at StructureSelector
        construction time, same "load at boot" pattern equity's
        calibration_thresholds already uses. Defaults to is_paper=True since
        options trades paper-only today; the caller passes the segment's
        actual current mode."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT structure_type, p_win_floor, p_win_ceiling "
                "FROM options_conviction_calibration WHERE is_paper = $1",
                is_paper,
            )
            return {
                r["structure_type"]: (float(r["p_win_floor"]), float(r["p_win_ceiling"]))
                for r in rows
            }

    # ── IV history ───────────────────────────────────────────────────────────

    async def record_atm_iv(
        self,
        underlying: str,
        atm_iv: float,
        snapshot_date: date | None = None,
        expiry: date | None = None,
        days_to_expiry: int | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO options_iv_history
                    (underlying, snapshot_date, atm_iv, expiry, days_to_expiry)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (underlying, snapshot_date) DO UPDATE
                    SET atm_iv = EXCLUDED.atm_iv,
                        expiry = EXCLUDED.expiry,
                        days_to_expiry = EXCLUDED.days_to_expiry
                """,
                underlying,
                snapshot_date or date.today(),
                float(atm_iv),
                expiry,
                days_to_expiry,
            )

    async def recent_atm_iv(self, underlying: str, limit: int = 252) -> list[float]:
        """Trailing ATM-IV samples (most recent first) for percentile computation."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT atm_iv FROM options_iv_history
                WHERE underlying = $1
                ORDER BY snapshot_date DESC
                LIMIT $2
                """,
                underlying,
                limit,
            )
        return [float(r["atm_iv"]) for r in rows]

    # ── seller-desk volatility intelligence (options_spec.md §2) ───────────────

    async def insert_vol_intel(
        self,
        symbol: str,
        timestamp: datetime,
        *,
        iv_30d: float | None,
        iv_realized_20d: float | None,
        iv_realized_30d: float | None,
        iv_realized_60d: float | None,
        ivr: float | None,
        ivp: float | None,
        vol_premium: float | None,
        term_structure: str | None,
        skew_25delta: float | None,
        gex: float | None,
        confidence: str,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO options_vol_intel_history (
                    symbol, date, timestamp, iv_30d, iv_realized_20d, iv_realized_30d,
                    iv_realized_60d, ivr, ivp, vol_premium, term_structure,
                    skew_25delta, gex, confidence
                ) VALUES ($1, $2::date, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (symbol, timestamp) DO NOTHING
                """,
                symbol,
                timestamp,
                iv_30d,
                iv_realized_20d,
                iv_realized_30d,
                iv_realized_60d,
                ivr,
                ivp,
                vol_premium,
                term_structure,
                skew_25delta,
                gex,
                confidence,
            )

    async def recent_vol_intel_ivs(self, symbol: str, limit: int = 252) -> list[float]:
        """Trailing iv_30d samples (most recent first) — feeds IVR computation."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT iv_30d FROM options_vol_intel_history
                WHERE symbol = $1 AND iv_30d IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT $2
                """,
                symbol,
                limit,
            )
        return [float(r["iv_30d"]) for r in rows]

    async def latest_term_structure(self, symbol: str) -> str | None:
        """Most recent non-null term_structure classification (VolatilityEngine's
        30-min tier2 chain scan) — feeds calendar_spread eligibility without an
        extra live chain fetch."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT term_structure FROM options_vol_intel_history
                WHERE symbol = $1 AND term_structure IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                symbol,
            )
        return str(row["term_structure"]) if row is not None else None

    async def insert_vol_surface_snapshot(
        self, symbol: str, timestamp: datetime, surface_data: dict[str, object]
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO options_vol_surface_snapshots (symbol, timestamp, surface_data)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (symbol, timestamp) DO NOTHING
                """,
                symbol,
                timestamp,
                json.dumps(surface_data),
            )

    # ── event calendar (options_spec.md §16) ────────────────────────────────────

    async def insert_event_blackout(
        self,
        event_type: str,
        *,
        event_date: date | None,
        underlying: str | None,
        layer: str,
        blackout_from: datetime | None,
        blackout_until: datetime | None,
        source: str,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO options_event_calendar (
                    event_type, event_date, underlying, layer,
                    blackout_from, blackout_until, source
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                event_type,
                event_date,
                underlying,
                layer,
                blackout_from,
                blackout_until,
                source,
            )

    # ── learning / autopsy (options_spec.md §18) ────────────────────────────────

    async def insert_learning_finding(
        self, position_id: str, question: str, finding: str, confidence: float | None
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO options_learning_log (position_id, question, finding, confidence)
                VALUES ($1, $2, $3, $4)
                """,
                position_id,
                question,
                finding,
                confidence,
            )

    async def active_blackouts(self, underlying: str, now: datetime) -> list[dict[str, object]]:
        """Rows relevant to `underlying` right now — dynamic rows active at `now`,
        plus scheduled rows with an event up to 21 days out (the widest window
        any structure, e.g. Iron Condor, needs) so callers can apply the correct
        per-structure blackout distance themselves."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT event_type, event_date, layer, blackout_from, blackout_until
                FROM options_event_calendar
                WHERE (underlying = $1 OR underlying IS NULL)
                  AND (
                        (layer = 'dynamic' AND $2 BETWEEN blackout_from AND blackout_until)
                        OR (layer = 'scheduled' AND event_date IS NOT NULL
                            AND event_date >= $2::date AND event_date <= ($2::date + 21))
                      )
                """,
                underlying,
                now,
            )
        return [dict(r) for r in rows]

    # ── seller-desk positions (migration 051) ───────────────────────────────
    # SellerDesk holds open positions in memory only; these three methods let
    # it survive a process restart (options_positions table existed unused
    # until this wiring — see seller_desk.py's restore_positions/_close_position).

    async def insert_position(
        self,
        position_id: str,
        *,
        underlying: str,
        strategy_type: str,
        protection_legs: list[OptionLeg],
        income_legs: list[OptionLeg],
        expiry: date,
        dte_at_entry: int,
        lot_size_at_entry: int,
        net_credit: float,
        max_loss: float,
        max_profit: float,
        entry_margin: float,
        entry_greeks: PortfolioGreeks | None,
        opened_at: datetime,
        is_paper: bool = True,
        entry_regime: str | None = None,
        entry_atm_iv: float | None = None,
        entry_spot: float | None = None,
        entry_short_delta: float | None = None,
    ) -> None:
        legs = {
            "protection": [_serialize_leg(leg) for leg in protection_legs],
            "income": [_serialize_leg(leg) for leg in income_legs],
        }
        g = entry_greeks
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO options_positions (
                    position_id, underlying, strategy_type, status, legs, expiry,
                    dte_at_entry, lot_size_at_entry, net_credit, max_loss, max_profit,
                    entry_margin, entry_delta, entry_gamma, entry_theta, entry_vega,
                    opened_at, is_paper,
                    entry_regime, entry_atm_iv, entry_spot, entry_short_delta
                ) VALUES (
                    $1, $2, $3, 'OPEN', $4::jsonb, $5, $6, $7, $8, $9, $10, $11,
                    $12, $13, $14, $15, $16, $17, $18, $19, $20, $21
                )
                ON CONFLICT (position_id) DO NOTHING
                """,
                position_id,
                underlying,
                strategy_type,
                json.dumps(legs),
                expiry,
                dte_at_entry,
                lot_size_at_entry,
                float(net_credit),
                float(max_loss),
                float(max_profit),
                float(entry_margin),
                float(g.net_delta) if g else None,
                float(g.net_gamma) if g else None,
                float(g.net_theta) if g else None,
                float(g.net_vega) if g else None,
                opened_at,
                is_paper,
                entry_regime,
                entry_atm_iv,
                entry_spot,
                entry_short_delta,
            )

    async def mark_breached(self, position_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE options_positions SET was_breached = TRUE WHERE position_id = $1",
                position_id,
            )

    async def update_peak_unrealized(self, position_id: str, peak: float) -> None:
        """Persist the unrealized-P&L high-water mark (never lowers an existing peak)."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE options_positions SET peak_unrealized_pnl = $2 "
                "WHERE position_id = $1 AND (peak_unrealized_pnl IS NULL OR peak_unrealized_pnl < $2)",
                position_id,
                peak,
            )

    async def upsert_pnl_attribution(
        self,
        day: date,
        *,
        net_delta: float,
        net_gamma: float,
        net_theta: float,
        net_vega: float,
        delta_pnl: float,
        gamma_pnl: float,
        theta_pnl: float,
        vega_pnl: float,
    ) -> None:
        """One row per day (options_pnl_attribution, migration 054, spec section 8)."""
        total = delta_pnl + gamma_pnl + theta_pnl + vega_pnl
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO options_pnl_attribution (
                    date, net_delta, net_gamma, net_theta, net_vega,
                    delta_pnl, gamma_pnl, theta_pnl, vega_pnl, total_pnl
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (date) DO UPDATE SET
                    net_delta = EXCLUDED.net_delta, net_gamma = EXCLUDED.net_gamma,
                    net_theta = EXCLUDED.net_theta, net_vega = EXCLUDED.net_vega,
                    delta_pnl = EXCLUDED.delta_pnl, gamma_pnl = EXCLUDED.gamma_pnl,
                    theta_pnl = EXCLUDED.theta_pnl, vega_pnl = EXCLUDED.vega_pnl,
                    total_pnl = EXCLUDED.total_pnl
                """,
                day,
                net_delta,
                net_gamma,
                net_theta,
                net_vega,
                delta_pnl,
                gamma_pnl,
                theta_pnl,
                vega_pnl,
                total,
            )

    async def close_position(
        self,
        position_id: str,
        *,
        exit_reason: str,
        closed_at: datetime,
        realized_pnl: float | None = None,
        exit_prices: dict[str, float] | None = None,
        charges_breakdown: dict[str, float] | None = None,
        broker: str | None = None,
    ) -> None:
        """``charges_breakdown``/``broker``: whole-structure itemised costs and the rate card used
        (realized_pnl stays GROSS; charges_taxes is their total).

        ``realized_pnl`` NULL means it couldn't be computed this close
        (e.g. an LTP fetch failed for one leg — see seller_desk._close_position)
        -- stored as NULL rather than a partially-wrong number, not silently
        defaulted to 0.

        ``exit_prices`` (tradingsymbol -> LTP at close) is written into each
        leg's JSON as ``exit_premium`` so Reports can show per-leg exit price
        and P&L; the total alone can't be split back into legs. Safe to add
        here: only OPEN rows are ever restored into OptionLeg objects."""
        async with self._pool.acquire() as conn, conn.transaction():
            if exit_prices:
                raw = await conn.fetchval(
                    "SELECT legs FROM options_positions WHERE position_id = $1", position_id
                )
                legs = json.loads(raw) if isinstance(raw, str) else (raw or {})
                for group in ("protection", "income"):
                    for leg in legs.get(group, []):
                        px = exit_prices.get(leg.get("tradingsymbol"))
                        if px is not None:
                            leg["exit_premium"] = px
                await conn.execute(
                    "UPDATE options_positions SET legs = $2::jsonb WHERE position_id = $1",
                    position_id,
                    json.dumps(legs),
                )
            await conn.execute(
                """
                UPDATE options_positions
                SET status = 'CLOSED', exit_reason = $2, closed_at = $3, realized_pnl = $4,
                    charges_breakdown = $5::jsonb, charges_taxes = $6, broker = $7
                WHERE position_id = $1
                """,
                position_id,
                exit_reason,
                closed_at,
                realized_pnl,
                json.dumps(charges_breakdown) if charges_breakdown else None,
                round(sum(charges_breakdown.values()), 2) if charges_breakdown else None,
                broker,
            )

    async def realized_pnl_closed_on(self, day: date, *, is_paper: bool) -> float:
        """Sum of realized_pnl for positions closed on IST calendar ``day`` (NULL
        realized_pnl rows -- incomplete closes -- contribute nothing)."""
        async with self._pool.acquire() as conn:
            total = await conn.fetchval(
                """
                SELECT COALESCE(SUM(realized_pnl), 0) FROM options_positions
                WHERE status = 'CLOSED' AND is_paper = $1
                  AND (closed_at AT TIME ZONE 'Asia/Kolkata')::date = $2
                """,
                is_paper,
                day,
            )
        return float(total or 0.0)

    async def realized_pnl_by_structure_closed_on(
        self, day: date, *, is_paper: bool
    ) -> dict[str, float]:
        """Sum of realized_pnl, GROUPED BY strategy_type (structure type, e.g.
        'bull_put_spread', 'weekly_condor_simple'), for positions closed on IST
        calendar ``day``. Lets the per-strategy daily-loss kill (SellerDesk) restore
        each strategy's today-so-far loss on restart without a new persisted column --
        same approach realized_pnl_closed_on already uses for the desk-wide figure."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT strategy_type, COALESCE(SUM(realized_pnl), 0) AS total
                FROM options_positions
                WHERE status = 'CLOSED' AND is_paper = $1
                  AND (closed_at AT TIME ZONE 'Asia/Kolkata')::date = $2
                GROUP BY strategy_type
                """,
                is_paper,
                day,
            )
        return {str(r["strategy_type"]): float(r["total"] or 0.0) for r in rows}

    async def save_daily_pnl_snapshots(
        self, snapshot_date: date, pnl_by_position: dict[str, float]
    ) -> None:
        """Baseline unrealized P&L for every still-OPEN position, captured once
        near market open each trading day (migration 086) -- the "Today's P&L"
        dashboard figure diffs against this. Idempotent per (position_id, date):
        a re-run the same day (e.g. after a restart) overwrites with the latest
        capture rather than creating a second row or silently skipping."""
        if not pnl_by_position:
            return
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO options_pnl_daily_snapshot (position_id, snapshot_date, unrealized_pnl)
                VALUES ($1, $2, $3)
                ON CONFLICT (position_id, snapshot_date)
                DO UPDATE SET unrealized_pnl = EXCLUDED.unrealized_pnl, captured_at = now()
                """,
                [(pid, snapshot_date, pnl) for pid, pnl in pnl_by_position.items()],
            )

    async def daily_pnl_baselines(
        self, position_ids: list[str], snapshot_date: date
    ) -> dict[str, float]:
        """This snapshot_date's baseline unrealized P&L per position_id. A
        position absent from the result has no snapshot for that day (opened
        intraday today, or the capture job hasn't run yet) -- the caller's
        Today's P&L then falls back to the position's current total P&L."""
        if not position_ids:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT position_id, unrealized_pnl FROM options_pnl_daily_snapshot
                WHERE snapshot_date = $1 AND position_id = ANY($2::text[])
                """,
                snapshot_date,
                position_ids,
            )
        return {str(r["position_id"]): float(r["unrealized_pnl"]) for r in rows}

    async def closed_positions_on(self, day: date, *, is_paper: bool) -> list[dict[str, Any]]:
        """Positions closed on IST calendar ``day`` (underlying, realized_pnl which
        is None for an incomplete close) -- the per-trade view of
        realized_pnl_closed_on, for trade counts / wins / best / worst."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT underlying, realized_pnl FROM options_positions
                WHERE status = 'CLOSED' AND is_paper = $1
                  AND (closed_at AT TIME ZONE 'Asia/Kolkata')::date = $2
                """,
                is_paper,
                day,
            )
        return [dict(r) for r in rows]

    async def closed_legs_pnl_on(self, day: date, *, is_paper: bool) -> float:
        """Realised rupees from legs closed on IST ``day`` inside positions that are still OPEN (per-leg
        stops). A position that has fully CLOSED already carries these in its realized_pnl, so only the
        OPEN ones are scanned -- together the two cover every realised rupee, whatever the strategy."""
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        ist = ZoneInfo("Asia/Kolkata")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT legs FROM options_positions WHERE status = 'OPEN' AND is_paper = $1",
                is_paper,
            )
        total = 0.0
        for r in rows:
            raw = r["legs"]
            legs = json.loads(raw) if isinstance(raw, str) else (raw or {})
            for c in legs.get("closed", []):
                try:
                    if datetime.fromisoformat(c["exit_at"]).astimezone(ist).date() != day:
                        continue
                    qty = int(c.get("lots", 1)) * int(c.get("lot_size", 1))
                    sign = 1.0 if c.get("direction") == "BUY" else -1.0
                    total += sign * (float(c["exit_premium"]) - float(c["premium"])) * qty
                except (KeyError, TypeError, ValueError):
                    continue
        return total

    async def delete_duplicate_open_positions(
        self,
        *,
        is_paper: bool,
        keep_per_underlying: int = 1,
        exclude_strategy_types: Sequence[str] = (),
    ) -> list[str]:
        """Self-healing invariant enforcement: at most one OPEN position per
        underlying (SellerDesk.run_cycle's own dedup guard assumes this holds).
        2026-09-11 incident: a process running pre-fix code opened 60 duplicate
        NIFTY bull_put_spread positions over 6.5 hours before anyone noticed --
        the in-memory guard alone is silent if it's ever bypassed (stale
        deploy, restart race, a future regression). This is the DB-level
        backstop, called every seller-desk cycle before any new trade is
        evaluated: for any underlying with more than one OPEN row, keeps the
        earliest (the one actually reflected in the executed order book) and
        DELETES the rest outright -- they never represented a real,
        distinctly-executed position to begin with, so there's no realized
        P&L or exit history worth keeping (operator's explicit call: hard
        delete over a soft 'duplicate_auto_reconciled' close, 2026-09-11).

        Since 2026-10-06 the allowance is ``keep_per_underlying`` (options_settings
        position_limits.max_strategies_per_index) instead of a fixed 1, so a
        legitimate 2nd strategy on the same index is never deleted; anything beyond
        the configured allowance is still treated as a runaway duplicate.

        ``exclude_strategy_types`` rows (hosted strategies with their own slot) are
        neither counted nor ever deleted.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH ranked AS (
                    SELECT position_id,
                           row_number() OVER (
                               PARTITION BY underlying ORDER BY opened_at ASC
                           ) AS rn
                    FROM options_positions
                    WHERE status = 'OPEN' AND is_paper = $1
                      AND strategy_type <> ALL($3::text[])
                )
                DELETE FROM options_positions
                WHERE position_id IN (SELECT position_id FROM ranked WHERE rn > $2)
                RETURNING position_id
                """,
                is_paper,
                max(1, int(keep_per_underlying)),
                list(exclude_strategy_types),
            )
        return [str(r["position_id"]) for r in rows]

    async def has_position_for_expiry(
        self, *, strategy_type: str, underlying: str, expiry: date, is_paper: bool
    ) -> bool:
        """True if ANY position (open or closed) of this type was opened for this
        underlying+expiry -- the weekly condor's once-per-expiry rule must survive
        a restart and an early take-profit, so it reads the DB, not memory."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                """
                SELECT 1 FROM options_positions
                WHERE strategy_type = $1 AND underlying = $2 AND expiry = $3 AND is_paper = $4
                LIMIT 1
                """,
                strategy_type,
                underlying,
                expiry,
                is_paper,
            )
        return row is not None

    async def has_position_opened_on(
        self, *, strategy_type: str, is_paper: bool, day: date
    ) -> bool:
        """True if ANY position (open or closed) of this type was opened on IST calendar ``day``
        -- options_intraday_short_premium's once-a-day rule must survive a restart and an early full stop-out."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                """
                SELECT 1 FROM options_positions
                WHERE strategy_type = $1 AND is_paper = $2
                  AND (opened_at AT TIME ZONE 'Asia/Kolkata')::date = $3
                LIMIT 1
                """,
                strategy_type,
                is_paper,
                day,
            )
        return row is not None

    async def set_legs_meta(self, position_id: str, meta: dict[str, Any]) -> None:
        """Merge ``meta`` into the position's legs JSON under "meta" (rules frozen at entry:
        per-leg SL %, exit time, LLM shadow verdict). Safe: restore only reads known keys."""
        async with self._pool.acquire() as conn, conn.transaction():
            raw = await conn.fetchval(
                "SELECT legs FROM options_positions WHERE position_id = $1 FOR UPDATE", position_id
            )
            legs = json.loads(raw) if isinstance(raw, str) else (raw or {})
            legs["meta"] = {**legs.get("meta", {}), **meta}
            await conn.execute(
                "UPDATE options_positions SET legs = $2::jsonb WHERE position_id = $1",
                position_id,
                json.dumps(legs),
            )

    async def record_leg_exit(self, position_id: str, record: dict[str, Any]) -> None:
        """Move one leg (matched by tradingsymbol) from the open groups to legs["closed"] with its
        exit price/time/reason, so a restart restores the position WITHOUT re-opening that leg and
        keeps its realized P&L. Used by options_intraday_short_premium's per-leg stops."""
        async with self._pool.acquire() as conn, conn.transaction():
            raw = await conn.fetchval(
                "SELECT legs FROM options_positions WHERE position_id = $1 FOR UPDATE", position_id
            )
            legs = json.loads(raw) if isinstance(raw, str) else (raw or {})
            for group in ("protection", "income"):
                legs[group] = [
                    leg
                    for leg in legs.get(group, [])
                    if leg.get("tradingsymbol") != record["tradingsymbol"]
                ]
            legs["closed"] = [*legs.get("closed", []), record]
            await conn.execute(
                "UPDATE options_positions SET legs = $2::jsonb WHERE position_id = $1",
                position_id,
                json.dumps(legs),
            )

    async def load_open_positions(self, is_paper: bool | None = None) -> list[dict[str, object]]:
        """All OPEN rows, for SellerDesk.restore_positions() to reconstruct
        in-memory position tracking after a process restart.

        is_paper=None (default) returns every open row regardless of mode. Phase 2
        passes the desk's own mode so a live-mode desk never restores a paper row
        (or vice versa) — every pre-migration-061 row defaults to is_paper=TRUE.
        """
        _where = "status = 'OPEN'" + ("" if is_paper is None else " AND is_paper = $1")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT position_id, underlying, strategy_type, legs, expiry,
                       net_credit, entry_margin, opened_at, adjustments_count,
                       entry_delta, entry_gamma, entry_theta, entry_vega,
                       entry_regime, entry_atm_iv, entry_spot, entry_short_delta, was_breached,
                       peak_unrealized_pnl, is_paper
                FROM options_positions
                WHERE {_where}
                """,  # noqa: S608 — _where is built only from trusted literals above
                *([] if is_paper is None else [is_paper]),
            )
        return [dict(r) for r in rows]

    async def record_execution(
        self,
        result: Any,
        protection_legs: list[OptionLeg],
        income_legs: list[OptionLeg],
        naked_detected: bool = False,
    ) -> None:
        """One options_execution_log row per execute_structure() attempt -- including the ones that
        failed (rejected protection leg, emergency close), which otherwise left NO trace outside the
        log file. `result` is an options_leg_executor.ExecutionResult."""
        states = list(result.protection_legs) + list(result.income_legs)
        legs = list(protection_legs) + list(income_legs)
        outcome = str(result.outcome)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO options_execution_log
                    (position_id, leg_count, legs_filled, legs_rejected, partial_fill,
                     naked_exposure_detected, emergency_action_taken, intended_prices)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                """,
                result.position_id,
                len(legs),
                sum(1 for s in states if s.filled),
                sum(1 for s in states if s.rejected),
                outcome.startswith("PARTIAL"),
                bool(naked_detected),
                "MARKET_CLOSE_FILLED_LEGS" if outcome == "PARTIAL_EMERGENCY_CLOSED" else None,
                json.dumps(
                    {"outcome": outcome, **{leg.tradingsymbol: leg.premium for leg in legs}}
                ),
            )

    async def record_adjustment(
        self,
        position_id: str,
        *,
        protection_legs: list[OptionLeg],
        income_legs: list[OptionLeg],
        old_leg: OptionLeg,
        new_leg: OptionLeg,
        adjustment_cost: float,
        remaining_premium: float,
        reason: str,
        adjustments_made: int,
        adjusted_at: datetime,
    ) -> None:
        """Persists a completed roll: updates ``legs``/``adjustments_count`` on
        options_positions (so a restart restores the rolled strike and the
        count that gates MAX_ADJUSTMENTS_PER_POSITION) and appends one audit
        row to options_adjustment_log (migration 065)."""
        legs = {
            "protection": [_serialize_leg(leg) for leg in protection_legs],
            "income": [_serialize_leg(leg) for leg in income_legs],
        }
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                UPDATE options_positions
                SET legs = $2::jsonb, adjustments_count = $3
                WHERE position_id = $1
                """,
                position_id,
                json.dumps(legs),
                adjustments_made,
            )
            await conn.execute(
                """
                INSERT INTO options_adjustment_log (
                    position_id, adjusted_at, old_tradingsymbol, old_strike,
                    new_tradingsymbol, new_strike, instrument_type,
                    adjustment_cost, remaining_premium, reason, adjustments_made
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                position_id,
                adjusted_at,
                old_leg.tradingsymbol,
                old_leg.strike,
                new_leg.tradingsymbol,
                new_leg.strike,
                new_leg.instrument_type,
                float(adjustment_cost),
                float(remaining_premium),
                reason,
                adjustments_made,
            )
