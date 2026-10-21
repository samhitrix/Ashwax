"""
Unit tests for src/db/repositories/ — all DB calls mocked.

Tests cover: insert, fetch, update paths for each repository.
No real Postgres connection is required.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── shared mock factory ───────────────────────────────────────────────────────


def _pool(fetchval: object = 1, fetch: list | None = None, fetchrow: object = None) -> MagicMock:
    """Return a minimal mock asyncpg pool."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.fetchval = AsyncMock(return_value=fetchval)
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow)

    tx_ctx = AsyncMock()
    tx_ctx.__aenter__ = AsyncMock(return_value=None)
    tx_ctx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx_ctx)

    pool = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


# ── TradeRepository ───────────────────────────────────────────────────────────


class TestTradeRepository:
    def _repo(self, **kw: object):  # type: ignore[no-untyped-def]
        from src.db.repositories.trades import TradeRepository

        return TradeRepository(_pool(**kw))

    async def test_insert_returns_id(self) -> None:
        repo = self._repo(fetchval=42)
        trade_id = await repo.insert(
            {"agent_trade_id": "T001", "symbol": "RELIANCE", "direction": "long", "quantity": 10}
        )
        assert trade_id == 42

    async def test_insert_paper_uses_paper_table(self) -> None:
        from src.db.repositories.trades import TradeRepository

        pool = _pool(fetchval=7)
        conn = pool.acquire.return_value.__aenter__.return_value
        repo = TradeRepository(pool)
        await repo.insert(
            {"agent_trade_id": "PT001", "symbol": "INFY", "direction": "long", "quantity": 5},
            is_paper=True,
        )
        sql = conn.fetchval.call_args[0][0]
        assert "paper_trades" in sql

    async def test_update_state_calls_execute(self) -> None:
        from src.db.repositories.trades import TradeRepository

        pool = _pool()
        conn = pool.acquire.return_value.__aenter__.return_value
        repo = TradeRepository(pool)
        await repo.update_state("T001", "FILLED")
        conn.execute.assert_called_once()
        sql = conn.execute.call_args[0][0]
        assert "order_state" in sql

    async def test_update_fill_calls_execute(self) -> None:
        from src.db.repositories.trades import TradeRepository

        pool = _pool()
        conn = pool.acquire.return_value.__aenter__.return_value
        repo = TradeRepository(pool)
        await repo.update_fill(
            "T001",
            Decimal("2751.50"),
            datetime(2026, 5, 31, 10, 23, tzinfo=UTC),
        )
        sql = conn.execute.call_args[0][0]
        assert "fill_price" in sql

    async def test_update_fill_never_reopens_a_closed_trade(self) -> None:
        """2026-09-23 regression guard: _price_truth_phase2_sweep corrects a fill
        price against a position snapshot taken at the top of a monitor cycle. If
        the position closed (a different async loop's force_exit) in the gap
        before this lands, the old unconditional order_state='FILLED' silently
        reopened an already-CLOSED trade on the dashboard -- observed live, AUBANK
        exited at 10:51:18 UTC and was clobbered back to FILLED at 10:51:52 UTC by
        exactly this call. The SQL must guard against overwriting a CLOSED row."""
        from src.db.repositories.trades import TradeRepository

        pool = _pool()
        conn = pool.acquire.return_value.__aenter__.return_value
        repo = TradeRepository(pool)
        await repo.update_fill(
            "T001",
            Decimal("2751.50"),
            datetime(2026, 5, 31, 10, 23, tzinfo=UTC),
        )
        sql = conn.execute.call_args[0][0]
        assert "order_state != 'CLOSED'" in sql

    async def test_update_pnl_adds_partial_booked_in_sql(self) -> None:
        """pnl_realized is the WHOLE trade: last-leg net + partial_pnl_booked, added
        inside the one UPDATE so no caller (close path, ghost reconciler, future code)
        can record only the last leg (2026-09-24 LAURUSLABS lost half its P&L)."""
        from src.db.repositories.trades import TradeRepository

        pool = _pool()
        conn = pool.acquire.return_value.__aenter__.return_value
        repo = TradeRepository(pool)
        await repo.update_pnl(
            "T001",
            Decimal("1812.14"),
            Decimal("66.36"),
            Decimal("2050"),
            datetime(2026, 9, 24, 12, 7, tzinfo=UTC),
            is_paper=True,
        )
        sql = conn.execute.call_args[0][0]
        assert "paper_trades" in sql
        assert "pnl_realized = $2 + COALESCE($6, partial_pnl_booked)" in sql

    async def test_update_pnl_can_write_partial_columns_atomically(self) -> None:
        """If the earlier best-effort partial persist failed, the close still writes
        the in-memory partial so the DB total is right."""
        from src.db.repositories.trades import TradeRepository

        pool = _pool()
        conn = pool.acquire.return_value.__aenter__.return_value
        repo = TradeRepository(pool)
        await repo.update_pnl(
            "T001",
            Decimal("1812.14"),
            Decimal("66.36"),
            Decimal("2050"),
            datetime(2026, 9, 24, 12, 7, tzinfo=UTC),
            partial_pnl_booked=Decimal("1812.14"),
            partial_qty_booked=13,
        )
        args = conn.execute.call_args[0]
        assert args[6] == Decimal("1812.14")
        assert args[7] == 13

    async def test_daily_realized_pnl_returns_decimal(self) -> None:
        repo = self._repo(fetchval=Decimal("3500.00"))
        pnl = await repo.daily_realized_pnl("2026-05-31")
        assert isinstance(pnl, Decimal)
        assert pnl == Decimal("3500.00")

    async def test_open_trades_returns_list(self) -> None:
        repo = self._repo(fetch=[MagicMock()])
        result = await repo.open_trades()
        assert isinstance(result, list)

    async def test_closed_trades_returns_list(self) -> None:
        repo = self._repo(fetch=[MagicMock(), MagicMock()])
        result = await repo.closed_trades(limit=10)
        assert len(result) == 2


# ── SupervisorRepository ──────────────────────────────────────────────────────


class TestSupervisorRepository:
    def _repo(self, **kw: object):  # type: ignore[no-untyped-def]
        from src.db.repositories.supervisor import SupervisorRepository

        return SupervisorRepository(_pool(**kw))

    async def test_emit_event_returns_id(self) -> None:
        repo = self._repo(fetchval=99)
        event_id = await repo.emit_event("SIGNAL_REJECTED", "crm", severity="WARNING")
        assert event_id == 99

    async def test_beat_calls_execute(self) -> None:
        from src.db.repositories.supervisor import SupervisorRepository

        pool = _pool()
        conn = pool.acquire.return_value.__aenter__.return_value
        repo = SupervisorRepository(pool)
        await repo.beat("crm", pid=1234)
        conn.execute.assert_called_once()

    async def test_open_incident_returns_id(self) -> None:
        repo = self._repo(fetchval=5)
        inc_id = await repo.open_incident("INV_C2_FLOOR", "INV_C2", "CRITICAL", "Floor breached")
        assert inc_id == 5

    async def test_resolve_incident_calls_execute(self) -> None:
        from src.db.repositories.supervisor import SupervisorRepository

        pool = _pool()
        conn = pool.acquire.return_value.__aenter__.return_value
        repo = SupervisorRepository(pool)
        await repo.resolve_incident("INV_C2_FLOOR")
        conn.execute.assert_called_once()

    async def test_get_flag_returns_bool(self) -> None:
        repo = self._repo(fetchval=True)
        val = await repo.get_flag("master_switch")
        assert val is True

    async def test_get_flag_raises_on_missing(self) -> None:
        repo = self._repo(fetchval=None)
        with pytest.raises(KeyError, match="master_switch"):
            await repo.get_flag("master_switch")

    async def test_set_flag_raises_on_missing_flag(self) -> None:
        from src.db.repositories.supervisor import SupervisorRepository

        pool = _pool()
        conn = pool.acquire.return_value.__aenter__.return_value
        conn.execute = AsyncMock(return_value="UPDATE 0")
        repo = SupervisorRepository(pool)
        with pytest.raises(KeyError):
            await repo.set_flag("nonexistent_flag", True)

    async def test_master_switch_on_delegates_to_get_flag(self) -> None:
        repo = self._repo(fetchval=True)
        assert await repo.master_switch_on() is True

    async def test_is_paper_mode_delegates_to_get_flag(self) -> None:
        repo = self._repo(fetchval=False)
        assert await repo.is_paper_mode() is False


# ── MarketDataRepository ──────────────────────────────────────────────────────


class TestMarketDataRepository:
    def _repo(self, **kw: object):  # type: ignore[no-untyped-def]
        from src.db.repositories.market_data import MarketDataRepository

        return MarketDataRepository(_pool(**kw))

    async def test_log_regime_returns_id(self) -> None:
        repo = self._repo(fetchval=10)
        rid = await repo.log_regime("BULL", vix=Decimal("16.5"))
        assert rid == 10

    async def test_latest_regime_returns_record(self) -> None:
        from src.db.repositories.market_data import MarketDataRepository

        mock_rec = MagicMock()
        pool = _pool(fetchrow=mock_rec)
        repo = MarketDataRepository(pool)
        result = await repo.latest_regime()
        assert result is mock_rec

    async def test_upsert_rs_calls_execute(self) -> None:
        from src.db.repositories.market_data import MarketDataRepository

        pool = _pool()
        conn = pool.acquire.return_value.__aenter__.return_value
        repo = MarketDataRepository(pool)
        await repo.upsert_rs("RELIANCE", Decimal("85.2"), percentile_rank=92)
        conn.execute.assert_called_once()

    async def test_upcoming_earnings_returns_list(self) -> None:
        repo = self._repo(fetch=[MagicMock()])
        result = await repo.upcoming_earnings(within_days=5)
        assert len(result) == 1


# ── NewsRepository ────────────────────────────────────────────────────────────


class TestNewsRepository:
    def _repo(self, **kw: object):  # type: ignore[no-untyped-def]
        from src.db.repositories.news import NewsRepository

        return NewsRepository(_pool(**kw))

    async def test_insert_returns_id(self) -> None:
        repo = self._repo(fetchval=3)
        nid = await repo.insert(
            headline="Test news",
            source="nseindia",
            event_type="CORPORATE",
            sentiment="NEUTRAL",
        )
        assert nid == 3

    async def test_recent_returns_list(self) -> None:
        repo = self._repo(fetch=[MagicMock(), MagicMock()])
        result = await repo.recent(limit=10)
        assert len(result) == 2

    async def test_for_symbol_passes_symbol_arg(self) -> None:
        from src.db.repositories.news import NewsRepository

        pool = _pool(fetch=[MagicMock()])
        conn = pool.acquire.return_value.__aenter__.return_value
        repo = NewsRepository(pool)
        await repo.for_symbol("RELIANCE")
        call_args = conn.fetch.call_args[0]
        assert "RELIANCE" in call_args


# ── LearningRepository ────────────────────────────────────────────────────────


class TestLearningRepository:
    def _repo(self, **kw: object):  # type: ignore[no-untyped-def]
        from src.db.repositories.learning import LearningRepository

        return LearningRepository(_pool(**kw))

    async def test_insert_insight_returns_id(self) -> None:
        repo = self._repo(fetchval=20)
        iid = await repo.insert_insight(
            trade_id=1,
            was_signal_valid=True,
            failure_was_environmental=False,
            what_i_would_do_differently="Wait for volume",
            generalizable_insight="VCP needs 3x volume",
            confidence_in_insight=Decimal("0.80"),
            applicable_when="VCP setup",
            regime_at_trade="BULL",
            sector_at_trade="IT",
            llm_model_used="gemini-2.0-flash",
            raw_llm_response="...",
        )
        assert iid == 20

    async def test_insert_attribution_returns_id(self) -> None:
        repo = self._repo(fetchval=15)
        aid = await repo.insert_attribution(1, "REGIME", "BULL boosted WR by 12%")
        assert aid == 15

    async def test_insert_calibration_returns_id(self) -> None:
        repo = self._repo(fetchval=7)
        cid = await repo.insert_calibration(
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            model_used="gemini-2.0-flash",
            confidence_bucket="0.7-0.8",
            predicted_wr=Decimal("0.72"),
            actual_wr=Decimal("0.68"),
            trade_count=25,
            brier_score=Decimal("0.042"),
        )
        assert cid == 7

    async def test_latest_calibration_returns_list(self) -> None:
        repo = self._repo(fetch=[MagicMock()])
        result = await repo.latest_calibration("gemini-2.0-flash")
        assert len(result) == 1


# ── ReportingRepository ───────────────────────────────────────────────────────


class TestReportingRepository:
    def _repo(self, **kw: object):  # type: ignore[no-untyped-def]
        from src.db.repositories.reporting import ReportingRepository

        return ReportingRepository(_pool(**kw))

    async def test_log_reconciliation_returns_id(self) -> None:
        repo = self._repo(fetchval=8)
        rid = await repo.log_reconciliation(
            recon_date=date(2026, 5, 31),
            db_capital=Decimal("198500"),
            broker_capital=Decimal("199200"),
        )
        assert rid == 8

    async def test_log_reconciliation_computes_drift(self) -> None:
        from src.db.repositories.reporting import ReportingRepository

        pool = _pool(fetchval=1)
        conn = pool.acquire.return_value.__aenter__.return_value
        repo = ReportingRepository(pool)
        await repo.log_reconciliation(
            recon_date=date(2026, 5, 31),
            db_capital=Decimal("199000"),
            broker_capital=Decimal("199500"),
        )
        # Drift = 500; verify it was passed to the query
        call_args = conn.fetchval.call_args[0]
        assert Decimal("500") in call_args

    async def test_append_audit_returns_id(self) -> None:
        repo = self._repo(fetchval=55)
        aid = await repo.append_audit(
            event_type="ORDER",
            event_data={"symbol": "RELIANCE", "qty": 10},
            trade_id=42,
        )
        assert aid == 55

    async def test_append_audit_uses_jsonb(self) -> None:
        from src.db.repositories.reporting import ReportingRepository

        pool = _pool(fetchval=1)
        conn = pool.acquire.return_value.__aenter__.return_value
        repo = ReportingRepository(pool)
        await repo.append_audit("ORDER", {"x": 1})
        sql = conn.fetchval.call_args[0][0]
        assert "jsonb" in sql.lower()

    async def test_log_corporate_action_returns_id(self) -> None:
        repo = self._repo(fetchval=3)
        cid = await repo.log_corporate_action(
            symbol="RELIANCE",
            action_type="SPLIT",
            action_date=date(2026, 6, 1),
            ratio_or_amount=Decimal("2"),
        )
        assert cid == 3

    async def test_recent_reconciliations_returns_list(self) -> None:
        repo = self._repo(fetch=[MagicMock()])
        result = await repo.recent_reconciliations()
        assert len(result) == 1


# ── OptionsRepository (seller-desk vol intelligence) ────────────────────────


class TestOptionsRepositoryVolIntel:
    def _repo(self, **kw: object):  # type: ignore[no-untyped-def]
        from src.db.repositories.options import OptionsRepository

        return OptionsRepository(_pool(**kw))

    async def test_insert_vol_intel_executes(self) -> None:
        repo = self._repo()
        pool = repo._pool  # type: ignore[attr-defined]
        conn = pool.acquire.return_value.__aenter__.return_value
        await repo.insert_vol_intel(
            "NIFTY",
            datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
            iv_30d=0.18,
            iv_realized_20d=0.15,
            iv_realized_30d=0.14,
            iv_realized_60d=0.16,
            ivr=62.0,
            ivp=58.0,
            vol_premium=0.04,
            term_structure="contango",
            skew_25delta=0.03,
            gex=1250000.0,
            confidence="FULL",
        )
        sql = conn.execute.call_args[0][0]
        assert "options_vol_intel_history" in sql

    async def test_recent_vol_intel_ivs_returns_floats(self) -> None:
        repo = self._repo(fetch=[{"iv_30d": Decimal("0.18")}, {"iv_30d": Decimal("0.20")}])
        ivs = await repo.recent_vol_intel_ivs("NIFTY")
        assert ivs == [0.18, 0.20]

    async def test_insert_vol_surface_snapshot_executes(self) -> None:
        repo = self._repo()
        pool = repo._pool  # type: ignore[attr-defined]
        conn = pool.acquire.return_value.__aenter__.return_value
        await repo.insert_vol_surface_snapshot(
            "NIFTY", datetime(2026, 7, 28, 10, 0, tzinfo=UTC), {"24000CE": {"iv": 0.18}}
        )
        sql = conn.execute.call_args[0][0]
        assert "options_vol_surface_snapshots" in sql


# ── OptionsRepository (event calendar) ──────────────────────────────────────


class TestOptionsRepositoryEventCalendar:
    def _repo(self, **kw: object):  # type: ignore[no-untyped-def]
        from src.db.repositories.options import OptionsRepository

        return OptionsRepository(_pool(**kw))

    async def test_insert_event_blackout_executes(self) -> None:
        repo = self._repo()
        pool = repo._pool  # type: ignore[attr-defined]
        conn = pool.acquire.return_value.__aenter__.return_value
        await repo.insert_event_blackout(
            "rbi_policy",
            event_date=date(2026, 8, 5),
            underlying=None,
            layer="scheduled",
            blackout_from=datetime(2026, 7, 29, tzinfo=UTC),
            blackout_until=datetime(2026, 8, 5, tzinfo=UTC),
            source="manual",
        )
        sql = conn.execute.call_args[0][0]
        assert "options_event_calendar" in sql

    async def test_active_blackouts_returns_dicts(self) -> None:
        repo = self._repo(fetch=[{"event_type": "rbi_policy", "event_date": date(2026, 8, 5)}])
        rows = await repo.active_blackouts("NIFTY", datetime(2026, 7, 29, tzinfo=UTC))
        assert rows == [{"event_type": "rbi_policy", "event_date": date(2026, 8, 5)}]


# ── OptionsRepository (adjustments) ─────────────────────────────────────────


class TestOptionsRepositoryAdjustments:
    def _repo(self, **kw: object):  # type: ignore[no-untyped-def]
        from src.db.repositories.options import OptionsRepository

        return OptionsRepository(_pool(**kw))

    def _leg(self, strike: float, instrument_type: str):  # type: ignore[no-untyped-def]
        from src.options.structure_selector import OptionLeg

        return OptionLeg(
            tradingsymbol=f"NFO:NIFTY{int(strike)}{instrument_type}",
            instrument_type=instrument_type,
            strike=strike,
            direction="SELL",
            lots=1,
            lot_size=25,
            premium=50.0,
            delta=0.2,
        )

    async def test_record_adjustment_updates_position_and_logs(self) -> None:
        repo = self._repo()
        pool = repo._pool  # type: ignore[attr-defined]
        conn = pool.acquire.return_value.__aenter__.return_value
        old_leg = self._leg(24000.0, "CE")
        new_leg = self._leg(23800.0, "CE")
        await repo.record_adjustment(
            "OPT-1",
            protection_legs=[],
            income_legs=[new_leg],
            old_leg=old_leg,
            new_leg=new_leg,
            adjustment_cost=42.0,
            remaining_premium=500.0,
            reason="cost=8% within 30% budget",
            adjustments_made=1,
            adjusted_at=datetime(2026, 9, 11, 10, 0, tzinfo=UTC),
        )
        calls = [c[0][0] for c in conn.execute.call_args_list]
        assert any("UPDATE options_positions" in sql for sql in calls)
        assert any("options_adjustment_log" in sql for sql in calls)

    async def test_load_open_positions_selects_adjustments_count(self) -> None:
        repo = self._repo(fetch=[{"position_id": "OPT-1", "adjustments_count": 1}])
        rows = await repo.load_open_positions()
        pool = repo._pool  # type: ignore[attr-defined]
        conn = pool.acquire.return_value.__aenter__.return_value
        sql = conn.fetch.call_args[0][0]
        assert "adjustments_count" in sql
        assert rows[0]["adjustments_count"] == 1


# ── OptionsRepository (learning/autopsy) ────────────────────────────────────


class TestOptionsRepositoryLearning:
    def _repo(self, **kw: object):  # type: ignore[no-untyped-def]
        from src.db.repositories.options import OptionsRepository

        return OptionsRepository(_pool(**kw))

    async def test_insert_learning_finding_executes(self) -> None:
        repo = self._repo()
        pool = repo._pool  # type: ignore[attr-defined]
        conn = pool.acquire.return_value.__aenter__.return_value
        await repo.insert_learning_finding(
            "OPT-ABC", "regime_correctness", "sold in trending regime", 0.7
        )
        sql = conn.execute.call_args[0][0]
        assert "options_learning_log" in sql


# ── OptionsRepository (exit tracking + conviction calibration, PR5/PR6) ─────


class TestOptionsRepositoryExitTracking:
    def _repo(self, **kw: object):  # type: ignore[no-untyped-def]
        from src.db.repositories.options import OptionsRepository

        return OptionsRepository(_pool(**kw))

    async def test_close_structure_executes_update(self) -> None:
        repo = self._repo()
        pool = repo._pool  # type: ignore[attr-defined]
        conn = pool.acquire.return_value.__aenter__.return_value
        await repo.close_structure("OPT-1", exit_reason="SL_FILLED", realized_pnl=-500.0)
        sql, args = conn.execute.call_args[0][0], conn.execute.call_args[0][1:]
        assert "UPDATE options_structures" in sql
        assert args[0] == "OPT-1"

    async def test_load_open_structures_no_filter(self) -> None:
        repo = self._repo(fetch=[{"structure_id": "OPT-1"}])
        rows = await repo.load_open_structures()
        assert rows == [{"structure_id": "OPT-1"}]

    async def test_load_open_structures_filters_by_is_paper(self) -> None:
        repo = self._repo(fetch=[{"structure_id": "OPT-1"}])
        pool = repo._pool  # type: ignore[attr-defined]
        conn = pool.acquire.return_value.__aenter__.return_value
        await repo.load_open_structures(is_paper=True)
        sql = conn.fetch.call_args[0][0]
        assert "is_paper" in sql

    async def test_load_closed_structures_with_outcome_filters_by_structure_type(self) -> None:
        repo = self._repo(fetch=[])
        pool = repo._pool  # type: ignore[attr-defined]
        conn = pool.acquire.return_value.__aenter__.return_value
        await repo.load_closed_structures_with_outcome("long_call")
        sql, args = conn.fetch.call_args[0][0], conn.fetch.call_args[0][1:]
        assert "realized_pnl IS NOT NULL" in sql
        assert args[0] == "long_call"

    async def test_upsert_conviction_calibration_executes(self) -> None:
        repo = self._repo()
        pool = repo._pool  # type: ignore[attr-defined]
        conn = pool.acquire.return_value.__aenter__.return_value
        await repo.upsert_conviction_calibration("long_call", 0.45, 0.68, 12, 0.21, 0.30)
        sql = conn.execute.call_args[0][0]
        assert "options_conviction_calibration" in sql

    async def test_load_conviction_calibration_returns_bounds_dict(self) -> None:
        repo = self._repo(
            fetch=[
                {
                    "structure_type": "long_call",
                    "p_win_floor": Decimal("0.45"),
                    "p_win_ceiling": Decimal("0.68"),
                }
            ]
        )
        bounds = await repo.load_conviction_calibration()
        assert bounds == {"long_call": (0.45, 0.68)}

    async def test_load_conviction_calibration_filters_by_is_paper(self) -> None:
        repo = self._repo(fetch=[])
        pool = repo._pool  # type: ignore[attr-defined]
        conn = pool.acquire.return_value.__aenter__.return_value
        await repo.load_conviction_calibration(is_paper=False)
        sql, args = conn.fetch.call_args[0][0], conn.fetch.call_args[0][1:]
        assert "is_paper" in sql
        assert args[0] is False

    async def test_upsert_conviction_calibration_defaults_is_paper_true(self) -> None:
        repo = self._repo()
        pool = repo._pool  # type: ignore[attr-defined]
        conn = pool.acquire.return_value.__aenter__.return_value
        await repo.upsert_conviction_calibration("long_call", 0.45, 0.68, 12, 0.21, 0.30)
        args = conn.execute.call_args[0][1:]
        assert args[0] == "long_call"
        assert args[1] is True  # is_paper default


# ── OHLCVRepository ─────────────────────────────────────────────────────────


class TestOHLCVRepositoryFetchDataframe:
    """2026-09-08: ohlcv_candles' numeric columns are Postgres NUMERIC, which
    asyncpg returns as Decimal — fetch_dataframe built the DataFrame straight
    from those rows with no coercion, so any downstream arithmetic against a
    plain float (an R-multiple, a config value, ...) raised TypeError
    ("unsupported operand type(s) for *: 'decimal.Decimal' and 'float'"),
    crashing vcp_swing's scan every single cycle."""

    def _repo(self, rows: list[dict]):  # type: ignore[no-untyped-def]
        from src.db.repositories.ohlcv import OHLCVRepository

        return OHLCVRepository(_pool(fetch=rows))

    def _decimal_rows(self) -> list[dict]:
        return [
            {
                "ts": datetime(2026, 9, 1, tzinfo=UTC),
                "open": Decimal("100.50"),
                "high": Decimal("105.25"),
                "low": Decimal("99.75"),
                "close": Decimal("104.00"),
                "volume": Decimal("123456"),
            },
            {
                "ts": datetime(2026, 9, 2, tzinfo=UTC),
                "open": Decimal("104.00"),
                "high": Decimal("108.00"),
                "low": Decimal("103.50"),
                "close": Decimal("107.25"),
                "volume": Decimal("98765"),
            },
        ]

    async def test_numeric_columns_are_float_not_decimal(self) -> None:
        import numpy as np

        repo = self._repo(self._decimal_rows())
        df = await repo.fetch_dataframe("CGCL", "1d", limit=10)
        for col in ("open", "high", "low", "close", "volume"):
            assert np.issubdtype(
                df[col].dtype, np.floating
            ), f"{col} is {df[col].dtype}, expected a float dtype"

    async def test_close_column_arithmetic_with_plain_float_does_not_raise(self) -> None:
        """The exact failure mode: Decimal * float raises, float * float doesn't."""
        repo = self._repo(self._decimal_rows())
        df = await repo.fetch_dataframe("CGCL", "1d", limit=10)
        risk = abs(float(df["close"].iloc[-1]) - float(df["open"].iloc[-1]))
        target_r = 3.0
        result = df["close"].iloc[-1] * target_r + risk  # must not raise TypeError
        assert isinstance(result, float)

    async def test_values_are_numerically_unchanged_by_the_cast(self) -> None:
        """This is a type coercion, not a precision fix — the actual price data
        (tick-size-driven, see reference_nse_tick_size_not_corruption) must come
        through byte-for-byte equal once represented as float."""
        repo = self._repo(self._decimal_rows())
        df = await repo.fetch_dataframe("CGCL", "1d", limit=10)
        assert df["close"].iloc[0] == pytest.approx(104.00)
        assert df["open"].iloc[1] == pytest.approx(104.00)

    async def test_empty_result_returns_empty_dataframe(self) -> None:
        repo = self._repo([])
        df = await repo.fetch_dataframe("CGCL", "1d", limit=10)
        assert df.empty


# ── OptionsRepository.insert_position (exec_broker, migration 089) ──────────


class TestOptionsInsertPositionExecBroker:
    async def _insert(self, **kw: object):  # type: ignore[no-untyped-def]
        from src.db.repositories.options import OptionsRepository

        pool = _pool()
        await OptionsRepository(pool).insert_position(
            "POS-1",
            underlying="NSE:NIFTY",
            strategy_type="short_strangle",
            protection_legs=[],
            income_legs=[],
            expiry=date(2026, 10, 20),
            dte_at_entry=5,
            lot_size_at_entry=65,
            net_credit=1.0,
            max_loss=2.0,
            max_profit=1.0,
            entry_margin=3.0,
            entry_greeks=None,
            opened_at=datetime(2026, 10, 15, tzinfo=UTC),
            **kw,
        )
        conn = pool.acquire.return_value.__aenter__.return_value
        sql, *params = conn.execute.await_args.args
        return sql, params

    async def test_exec_broker_is_written_as_the_last_param(self) -> None:
        import re

        sql, params = await self._insert(exec_broker="angel_one")
        assert "exec_broker" in sql
        assert params[-1] == "angel_one"
        # every placeholder is bound, none skipped: $1..$N matches the param count
        assert max(int(n) for n in re.findall(r"\$(\d+)", sql)) == len(params)
        # column list and VALUES tuple have the same arity (the literal 'OPEN' is one value)
        cols = sql.split("INSERT INTO options_positions (")[1].split(") VALUES")[0]
        vals = sql.split("VALUES (")[1].split(")\n                ON CONFLICT")[0]
        assert len(cols.split(",")) == len(vals.split(","))

    async def test_exec_broker_defaults_to_none(self) -> None:
        _, params = await self._insert()
        assert params[-1] is None
