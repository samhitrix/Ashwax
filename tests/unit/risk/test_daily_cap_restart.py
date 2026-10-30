"""Day-scoped CRM counters must survive a mid-day launcher restart.

These counters (new_trades_today, open_position_count, daily_loss_so_far,
consecutive_losses) are held in-memory in the CRMContext. A restart used to reset
them to 0, silently bypassing the caps — 6 trades before the 10:21 restart + 6
after = 12 on 2026-06-18 (cap is 6). The fix sources every counter from the DB
(paper_trades, the source of truth) on each scan cycle, so they are correct for
the whole day and across restarts.

G7's enforcement of the restored count is proven separately in test_crm.py
(``test_g7_blocks_when_max_daily_trades_reached``): new_trades_today=6 → BLOCK.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.orchestrator import _refresh_daily_counters


class _PaperEngine:
    def __init__(self, counters: dict[str, float]) -> None:
        self._counters = counters

    async def daily_risk_counters(self, table: str = "paper_trades") -> dict[str, float]:
        return self._counters


def _ctx() -> SimpleNamespace:
    # Fresh process after a restart: every counter starts at 0 in memory.
    # Phase 2's mode_*/mode_engine_* dicts must be present (writable) too — a real
    # CRMContext has them via field(default_factory=dict); this fixture fakes only
    # what _refresh_daily_counters actually touches.
    return SimpleNamespace(
        new_trades_today=0,
        open_position_count=0,
        daily_loss_so_far=0.0,
        consecutive_losses=0,
        mode_new_trades_today={},
        mode_open_position_count={},
        mode_daily_loss_so_far={},
        mode_consecutive_losses={},
        mode_engine_trades_today={},
        mode_engine_open_positions={},
        intraday_trades_today=0,
        swing_trades_today=0,
        intraday_open_positions=0,
        swing_open_positions=0,
        per_strategy_trades_today={},
        open_exposure_rupees=0.0,
        max_daily_loss=0.0,
    )


async def test_restart_restores_all_counters_from_db() -> None:
    # The DB already reflects the day's activity before the restart.
    pe = _PaperEngine(
        {
            "new_trades_today": 6,
            "open_position_count": 4,
            "daily_loss_so_far": 2819.17,
            "consecutive_losses": 3,
        }
    )
    ctx = _ctx()

    await _refresh_daily_counters({"paper_ledger": pe}, ctx)

    # Counter restored → CRM G7 (max=6) blocks the 7th trade; loss/streak gates live too.
    assert ctx.new_trades_today == 6
    assert ctx.open_position_count == 4
    assert ctx.daily_loss_so_far == 2819.17
    assert ctx.consecutive_losses == 3


async def test_fresh_day_all_zero() -> None:
    # Genuinely new trading day: no trades yet → DB counts are 0, caps reset cleanly.
    pe = _PaperEngine(
        {
            "new_trades_today": 0,
            "open_position_count": 0,
            "daily_loss_so_far": 0.0,
            "consecutive_losses": 0,
        }
    )
    ctx = _ctx()
    ctx.new_trades_today, ctx.open_position_count = 6, 4
    ctx.daily_loss_so_far, ctx.consecutive_losses = 99.0, 2

    await _refresh_daily_counters({"paper_ledger": pe}, ctx)

    assert ctx.new_trades_today == 0
    assert ctx.open_position_count == 0
    assert ctx.daily_loss_so_far == 0.0
    assert ctx.consecutive_losses == 0


async def test_live_mode_uses_order_monitor_for_open_count() -> None:
    # No paper engine (live): open-position count + deployed exposure from the monitor.
    positions = [
        SimpleNamespace(quantity=10, fill_price=100.0),
        SimpleNamespace(quantity=5, fill_price=200.0),
        SimpleNamespace(quantity=2, fill_price=50.0),
    ]
    om = SimpleNamespace(get_monitored=lambda: positions)
    ctx = _ctx()

    await _refresh_daily_counters({"order_monitor": om}, ctx)

    assert ctx.open_position_count == 3
    # operator.json max_total_exposure guard needs live deployed value
    assert ctx.open_exposure_rupees == 10 * 100 + 5 * 200 + 2 * 50  # 2100


async def test_no_engine_no_monitor_leaves_counters_untouched() -> None:
    ctx = SimpleNamespace(new_trades_today=5, open_position_count=2)

    await _refresh_daily_counters({}, ctx)

    assert ctx.new_trades_today == 5
    assert ctx.open_position_count == 2


# ── DB-derived semantics (loss sign + trailing streak) ──────────────────────────


class _FakeConn:
    def __init__(
        self,
        row: dict,
        closed: list[dict],
        per_strat: list[dict] | None = None,
        open_rows: list[dict] | None = None,
    ) -> None:
        self._row, self._closed = row, closed
        self._per_strat = per_strat or []
        self._open_rows = open_rows or []

    async def fetchrow(self, _sql: str) -> dict:
        return self._row

    async def fetch(self, sql: str) -> list[dict]:
        if "GROUP BY strategy_name" in sql:
            return self._per_strat
        # Open-position query for mark-to-market unrealized P&L.
        if "symbol, direction, quantity, fill_price" in sql:
            return self._open_rows
        return self._closed


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _FakePool:
    def __init__(
        self,
        row: dict,
        closed: list[dict],
        per_strat: list[dict] | None = None,
        open_rows: list[dict] | None = None,
    ) -> None:
        self._conn = _FakeConn(row, closed, per_strat, open_rows)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


class _FakeBroker:
    """Returns a fixed LTP map for mark-to-market pricing of open positions."""

    def __init__(self, ltp: dict[str, float]) -> None:
        self._ltp = ltp

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return {s: self._ltp[s] for s in symbols if s in self._ltp}


async def test_daily_risk_counters_loss_sign_and_trailing_streak() -> None:
    from src.paper_trading.paper_engine import PaperTradingEngine

    row = {"placed": 12, "net_pnl": -2819.17}
    # ordered by exit_timestamp DESC (most recent first): two losses, then a win
    # that breaks the streak, then an earlier loss that must NOT be counted.
    closed = [
        {"pnl_realized": -100.0},
        {"pnl_realized": -237.36},
        {"pnl_realized": 180.83},
        {"pnl_realized": -50.0},
    ]
    eng = PaperTradingEngine.__new__(PaperTradingEngine)
    eng._pool = _FakePool(row, closed)  # type: ignore[attr-defined]

    c = await eng.daily_risk_counters()

    assert c["new_trades_today"] == 12
    assert c["daily_loss_so_far"] == 2819.17  # net negative P&L → positive loss figure
    assert c["consecutive_losses"] == 2  # only the trailing run, win breaks it


async def test_daily_risk_counters_net_profit_means_zero_loss() -> None:
    from src.paper_trading.paper_engine import PaperTradingEngine

    open_rows = [
        {
            "symbol": "AAA",
            "direction": "buy",
            "quantity": 1,
            "fill_price": 100.0,
            "strategy_name": "minervini_vcp",
        },
    ]
    eng = PaperTradingEngine.__new__(PaperTradingEngine)
    eng._pool = _FakePool({"placed": 3, "net_pnl": 500.0}, [], open_rows=open_rows)  # type: ignore[attr-defined]
    eng._broker = _FakeBroker({})  # type: ignore[attr-defined]

    c = await eng.daily_risk_counters()

    assert c["daily_loss_so_far"] == 0.0
    assert c["open_position_count"] == 1


async def test_daily_risk_counters_includes_per_strategy() -> None:
    from src.paper_trading.paper_engine import PaperTradingEngine

    row = {"placed": 3, "net_pnl": 0.0}
    per_strat = [
        {"strategy_name": "orb_intraday_smart", "n": 2},
        {"strategy_name": "minervini_vcp", "n": 1},
    ]
    eng = PaperTradingEngine.__new__(PaperTradingEngine)
    eng._pool = _FakePool(row, [], per_strat)  # type: ignore[attr-defined]

    c = await eng.daily_risk_counters()

    assert c["per_strategy"]["orb_intraday_smart"] == 2
    assert c["per_strategy"]["minervini_vcp"] == 1


async def test_daily_risk_counters_open_position_count_ignores_entry_date() -> None:
    # A swing position entered days ago and still open must still be counted — the
    # open-position query has no entry-date filter, unlike the today-scoped queries
    # above (root cause of swing positions silently bypassing the G7 open-count cap).
    from src.paper_trading.paper_engine import PaperTradingEngine

    open_rows = [
        {
            "symbol": "LT",
            "direction": "buy",
            "quantity": 5,
            "fill_price": 4000.0,
            "strategy_name": "minervini_vcp",
        },
        {
            "symbol": "ITC",
            "direction": "buy",
            "quantity": 50,
            "fill_price": 290.0,
            "strategy_name": "orb_intraday_top",
        },
    ]
    eng = PaperTradingEngine.__new__(PaperTradingEngine)
    eng._pool = _FakePool({"placed": 0, "net_pnl": 0.0}, [], open_rows=open_rows)  # type: ignore[attr-defined]
    eng._broker = _FakeBroker({})  # type: ignore[attr-defined]

    c = await eng.daily_risk_counters()

    assert c["open_position_count"] == 2
    assert c["open_per_strategy"] == {"minervini_vcp": 1, "orb_intraday_top": 1}


# ── Mark-to-market: unrealized loss on OPEN positions folds into daily loss ──────


async def test_daily_loss_folds_unrealized_on_open_positions() -> None:
    # Realized net is small (-500), but two open positions are deep underwater.
    # The realized-only cap would have shown only 500; the MTM figure must include
    # the unrealized loss so the breaker can see money currently at risk.
    from src.paper_trading.paper_engine import PaperTradingEngine

    row = {"placed": 5, "net_pnl": -500.0}
    open_rows = [
        # long: fill 100, ltp 90 → -10 * 100 = -1000
        {
            "symbol": "AAA",
            "direction": "buy",
            "quantity": 100,
            "fill_price": 100.0,
            "strategy_name": "minervini_vcp",
        },
        # short: fill 200, ltp 210 → (210-200) * 50 * -1 = -500
        {
            "symbol": "BBB",
            "direction": "sell",
            "quantity": 50,
            "fill_price": 200.0,
            "strategy_name": "minervini_vcp",
        },
    ]
    eng = PaperTradingEngine.__new__(PaperTradingEngine)
    eng._pool = _FakePool(row, [], open_rows=open_rows)  # type: ignore[attr-defined]
    eng._broker = _FakeBroker({"AAA": 90.0, "BBB": 210.0})  # type: ignore[attr-defined]

    c = await eng.daily_risk_counters()

    # realized -500 + unrealized (-1000 -500) = -2000 → loss figure 2000
    assert c["unrealized_pnl"] == -1500.0
    assert c["daily_loss_so_far"] == 2000.0


async def test_daily_loss_open_profit_offsets_realized_loss() -> None:
    # An open position in profit reduces the day's mark-to-market loss.
    from src.paper_trading.paper_engine import PaperTradingEngine

    row = {"placed": 3, "net_pnl": -800.0}
    open_rows = [
        {
            "symbol": "AAA",
            "direction": "buy",
            "quantity": 100,
            "fill_price": 100.0,
            "strategy_name": "minervini_vcp",
        },
    ]
    eng = PaperTradingEngine.__new__(PaperTradingEngine)
    eng._pool = _FakePool(row, [], open_rows=open_rows)  # type: ignore[attr-defined]
    eng._broker = _FakeBroker({"AAA": 106.0})  # +6 * 100 = +600

    c = await eng.daily_risk_counters()

    # realized -800 + unrealized +600 = -200 → loss 200
    assert c["daily_loss_so_far"] == 200.0


async def test_daily_loss_broker_unavailable_falls_back_to_realized() -> None:
    # Broker LTP fetch failure must never crash the risk refresh — fall back to
    # realized-only rather than fabricate a price.
    from src.paper_trading.paper_engine import PaperTradingEngine

    class _BrokenBroker:
        async def get_ltp(self, _symbols: list[str]) -> dict[str, float]:
            raise RuntimeError("broker down")

    row = {"placed": 2, "net_pnl": -1200.0}
    open_rows = [
        {
            "symbol": "AAA",
            "direction": "buy",
            "quantity": 10,
            "fill_price": 100.0,
            "strategy_name": "minervini_vcp",
        }
    ]
    eng = PaperTradingEngine.__new__(PaperTradingEngine)
    eng._pool = _FakePool(row, [], open_rows=open_rows)  # type: ignore[attr-defined]
    eng._broker = _BrokenBroker()  # type: ignore[attr-defined]

    c = await eng.daily_risk_counters()

    assert c["daily_loss_so_far"] == 1200.0  # realized only


# ── Daily-loss breaker: force-flatten at the cap, once per day ───────────────────


class _FlattenEngine:
    """Records square_off_open calls so the breaker's action is observable."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def square_off_open(
        self,
        _repo: object,
        *,
        reason: str,
        price_mode: str,
        order_monitors: list[object] | None = None,
    ) -> int:
        self.calls.append({"reason": reason, "price_mode": price_mode})
        return 3


def _breaker_ctx(loss: float, cap: float = 4000.0) -> SimpleNamespace:
    return SimpleNamespace(daily_loss_so_far=loss, max_daily_loss=cap)


async def test_breaker_flattens_at_cap() -> None:
    from src.orchestrator import _enforce_daily_loss_breaker

    pe = _FlattenEngine()
    shared: dict = {}
    await _enforce_daily_loss_breaker(shared, _breaker_ctx(4200.0), pe)

    assert len(pe.calls) == 1
    assert pe.calls[0]["reason"] == "DAILY_LOSS_BREAKER"
    assert pe.calls[0]["price_mode"] == "ltp"


async def test_breaker_does_not_fire_below_cap() -> None:
    from src.orchestrator import _enforce_daily_loss_breaker

    pe = _FlattenEngine()
    await _enforce_daily_loss_breaker({}, _breaker_ctx(3999.0), pe)

    assert pe.calls == []


async def test_breaker_fires_once_per_day() -> None:
    from src.orchestrator import _enforce_daily_loss_breaker

    pe = _FlattenEngine()
    shared: dict = {}
    await _enforce_daily_loss_breaker(shared, _breaker_ctx(5000.0), pe)
    # Next cycle, still over cap — must NOT flatten again (positions already closed).
    await _enforce_daily_loss_breaker(shared, _breaker_ctx(5000.0), pe)

    assert len(pe.calls) == 1


# ── Intraday-only daily-loss breaker, per mode (2026-10-07) ──────────────────────


def _intraday_breaker_ctx(loss: float, cap: float = 3000.0, mode: str = "paper") -> SimpleNamespace:
    return SimpleNamespace(
        max_intraday_daily_loss=cap, mode_intraday_daily_loss_so_far={mode: loss}
    )


async def test_intraday_breaker_flattens_paper_at_cap() -> None:
    from src.orchestrator import _enforce_intraday_daily_loss_breaker

    pe = _FlattenEngine()
    shared: dict = {}
    await _enforce_intraday_daily_loss_breaker(shared, _intraday_breaker_ctx(3200.0), "paper", pe)
    assert len(pe.calls) == 1
    assert pe.calls[0]["reason"] == "INTRADAY_DAILY_LOSS_BREAKER"
    assert pe.calls[0]["price_mode"] == "ltp"


async def test_intraday_breaker_does_not_fire_below_cap() -> None:
    from src.orchestrator import _enforce_intraday_daily_loss_breaker

    pe = _FlattenEngine()
    await _enforce_intraday_daily_loss_breaker({}, _intraday_breaker_ctx(2999.0), "paper", pe)
    assert pe.calls == []


async def test_intraday_breaker_fires_once_per_day_per_mode() -> None:
    from src.orchestrator import _enforce_intraday_daily_loss_breaker

    pe = _FlattenEngine()
    shared: dict = {}
    await _enforce_intraday_daily_loss_breaker(shared, _intraday_breaker_ctx(5000.0), "paper", pe)
    await _enforce_intraday_daily_loss_breaker(shared, _intraday_breaker_ctx(5000.0), "paper", pe)
    assert len(pe.calls) == 1


async def test_intraday_breaker_live_mode_never_calls_square_off_open() -> None:
    # LIVE must NEVER call square_off_open (it writes to paper_trades) -- live only
    # alerts; G1 already blocks new live entries once the cap is breached.
    from src.orchestrator import _enforce_intraday_daily_loss_breaker

    pe = _FlattenEngine()
    alerts: list[dict] = []
    import src.infrastructure.operator_alerts as oa

    def _fake_alert(message, **kw):  # type: ignore[no-untyped-def]
        alerts.append({"message": message, **kw})
        return True

    orig = oa.raise_alert
    oa.raise_alert = _fake_alert  # type: ignore[assignment]
    try:
        await _enforce_intraday_daily_loss_breaker(
            {}, _intraday_breaker_ctx(3200.0, mode="live"), "live", pe
        )
    finally:
        oa.raise_alert = orig  # type: ignore[assignment]
    assert pe.calls == []  # no real-money action taken
    assert len(alerts) == 1
    assert alerts[0]["tier"] == "CRITICAL"
    assert alerts[0]["source"] == "intraday_daily_loss_live"


async def test_intraday_breaker_paper_and_live_are_independent_flags() -> None:
    # Paper firing must not suppress a later live breach the same day, and vice versa.
    from src.orchestrator import _enforce_intraday_daily_loss_breaker

    pe = _FlattenEngine()
    shared: dict = {}
    await _enforce_intraday_daily_loss_breaker(shared, _intraday_breaker_ctx(5000.0), "paper", pe)
    assert len(pe.calls) == 1
    import src.infrastructure.operator_alerts as oa

    orig = oa.raise_alert
    oa.raise_alert = lambda *a, **k: True  # type: ignore[assignment]
    try:
        await _enforce_intraday_daily_loss_breaker(
            shared, _intraday_breaker_ctx(5000.0, mode="live"), "live", pe
        )
    finally:
        oa.raise_alert = orig  # type: ignore[assignment]
    assert len(pe.calls) == 1  # live path never touches square_off_open regardless


async def test_intraday_breaker_zero_cap_is_a_noop() -> None:
    from src.orchestrator import _enforce_intraday_daily_loss_breaker

    pe = _FlattenEngine()
    await _enforce_intraday_daily_loss_breaker(
        {}, _intraday_breaker_ctx(10_000.0, cap=0.0), "paper", pe
    )
    assert pe.calls == []


# ── Failed refresh fails the CRM closed (2026-10-26 scan) ──────────────────────
# Each of these failures used to be `except Exception: pass`, leaving the caps on 0
# after a restart -- the 2026-09-22 cap-breach class.


class _BrokenPaperEngine:
    async def daily_risk_counters(self, **_kw: object) -> dict[str, float]:
        raise RuntimeError("db down")


def test_open_risk_recompute_failure_is_reported() -> None:
    from src.orchestrator import _update_open_risk_and_sector_exposure

    ctx = SimpleNamespace(open_risk_rupees=0.0, sector_open_exposure_rupees={})
    bad = SimpleNamespace(symbol="X", quantity=None, fill_price=None, sl_price=None)
    assert _update_open_risk_and_sector_exposure(ctx, [bad]) != ""
    assert _update_open_risk_and_sector_exposure(ctx, []) == ""


async def test_counter_reseed_failure_flags_then_clears() -> None:
    ctx = _ctx()
    await _refresh_daily_counters({"paper_ledger": _BrokenPaperEngine()}, ctx)
    assert "db down" in ctx.risk_inputs_unverified

    good = _PaperEngine(
        {
            "new_trades_today": 1,
            "open_position_count": 0,
            "daily_loss_so_far": 0.0,
            "consecutive_losses": 0,
        }
    )
    await _refresh_daily_counters({"paper_ledger": good}, ctx)
    assert ctx.risk_inputs_unverified == ""
    assert ctx.new_trades_today == 1
