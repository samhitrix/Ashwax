"""Integration test for the orchestrator's Phase 6 routing glue.

Exercises `_try_options_structure` with the real selector / gate / executor wired
exactly as `_run` wires them, against a fake broker — no live launch needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.brokers.base import Instrument, OptionQuote, OrderRequest
from src.options.greeks import bs_price, days_to_years
from src.options.nfo_chain import NFOChainProvider
from src.options.options_executor import OptionsExecutor
from src.options.options_risk import OptionsRiskGate
from src.options.structure_selector import StructureSelector
from src.orchestrator import _try_options_structure

_IST = ZoneInfo("Asia/Kolkata")
_EXPIRY = datetime.now(_IST) + timedelta(days=30)
_SPOT = 2750.0
_IV = 0.22
_R = 0.065
_STRIKES = [2650.0, 2700.0, 2750.0, 2800.0, 2850.0, 2900.0, 2950.0, 3000.0]

_CFG = {
    "enabled": True,
    "max_premium_at_risk_rupees": 5000,
    "max_delta_exposure": 0.7,
    "preferred_strategies": ["bull_call_spread", "long_call"],
    "avoid_naked_selling": True,
    "min_days_to_expiry": 14,
    "max_iv_percentile_for_buying": 70,
    "liquidity_min_oi": 1000,
    "liquidity_min_volume": 500,
}


class _FakeBroker:
    def __init__(self) -> None:
        self.placed: list[OrderRequest] = []

    async def get_instruments(self, exchange: str) -> list[Instrument]:
        return [
            Instrument(
                symbol=f"RELIANCE24JUL{int(k)}{kind}",
                exchange="NFO",
                instrument_token=f"{int(k)}{kind}",
                instrument_type=kind,
                lot_size=50,
                tick_size=0.05,
                expiry=_EXPIRY,
                strike=k,
                name="RELIANCE",
            )
            for k in _STRIKES
            for kind in ("CE", "PE")
        ]

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return {symbols[0]: _SPOT}

    async def get_option_quotes(self, symbols: list[str]) -> dict[str, OptionQuote]:
        t = days_to_years(30)
        out: dict[str, OptionQuote] = {}
        for sym in symbols:
            kind = "CE" if sym.endswith("CE") else "PE"
            strike = float(sym[-len("2750CE") : -2])
            price = bs_price(_SPOT, strike, t, _R, _IV, kind)
            out[sym] = OptionQuote(sym, price, price - 0.5, price + 0.5, oi=5000, volume=2000)
        return out

    async def place_order(self, order: OrderRequest) -> str:
        self.placed.append(order)
        return f"OID-{len(self.placed)}"


class _FakeRepo:
    async def recent_atm_iv(self, underlying: str, limit: int = 252) -> list[float]:
        return [0.15 + 0.0075 * i for i in range(20)]  # straddles 0.22 → pct < 70

    async def insert_structure(self, *a: object, **k: object) -> None:
        pass


def _components(broker: _FakeBroker) -> dict:
    return {
        "enabled": True,
        "cfg": _CFG,
        "provider": NFOChainProvider(broker, risk_free_rate=_R),
        "selector": StructureSelector(_CFG),
        "gate": OptionsRiskGate(_CFG),
        "executor": OptionsExecutor(broker, _FakeRepo()),
        "repo": _FakeRepo(),
    }


def _crm_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        max_risk_per_trade=3000.0,
        can_trade_options=True,
        open_position_count=1,
        max_open_positions=5,
    )


def _strategy(product: str = "DELIVERY") -> SimpleNamespace:
    meta = SimpleNamespace(product=product, name="vcp")
    return SimpleNamespace(spec=SimpleNamespace(meta=meta, name="vcp"))


async def _route(
    shared: dict,
    *,
    product: str = "DELIVERY",
    direction: str = "BUY",
    conviction: float = 0.6,
    paper_mode: bool = False,
    paper_engine=None,
) -> bool:
    return await _try_options_structure(
        shared=shared,
        crm_ctx=_crm_ctx(),
        strategy=_strategy(product),
        symbol="NSE:RELIANCE",
        direction=direction,
        entry=_SPOT,
        sl_pct=0.025,
        tgt_pct=0.073,
        conviction=conviction,
        earnings_days=999,
        signal_id="SIG1",
        paper_mode=paper_mode,
        paper_engine=paper_engine,
    )


# ── live routing ─────────────────────────────────────────────────────────────


async def test_live_routes_and_places_options_structure() -> None:
    broker = _FakeBroker()
    shared = {"options": _components(broker)}
    placed = await _route(shared)
    assert placed is True
    # NFO legs were placed (entry legs + SL on the long leg).
    assert any(o.exchange == "NFO" for o in broker.placed)
    assert any(o.tag == "opt-sl" for o in broker.placed)


async def test_options_disabled_falls_back_to_equity() -> None:
    placed = await _route({"options": None})
    assert placed is False


async def test_short_signal_falls_back_to_equity() -> None:
    broker = _FakeBroker()
    placed = await _route({"options": _components(broker)}, direction="SELL")
    assert placed is False
    assert broker.placed == []


async def test_intraday_product_falls_back_to_equity() -> None:
    broker = _FakeBroker()
    placed = await _route({"options": _components(broker)}, product="MIS")
    assert placed is False
    assert broker.placed == []


async def test_high_conviction_falls_back_to_equity() -> None:
    broker = _FakeBroker()
    # §11: high conviction + tight setup → equity participation, not options.
    placed = await _route({"options": _components(broker)}, conviction=0.9)
    assert placed is False


async def test_paper_mode_routes_and_places_options_structure() -> None:
    # Paper mode runs the SAME routing as live — chain fetch and structure
    # selection are identical. Safety comes from the injected broker: in the
    # real system paper_mode wires a PaperAdapter (real market data, fills
    # simulated locally, nothing sent to the live broker), not a special-case
    # here. This fake broker stands in for that safe simulation.
    broker = _FakeBroker()
    quote_calls: list[int] = []
    _orig = broker.get_option_quotes

    async def _spy(symbols: list[str]):  # type: ignore[no-untyped-def]
        quote_calls.append(len(symbols))
        return await _orig(symbols)

    broker.get_option_quotes = _spy  # type: ignore[method-assign]
    placed = await _route({"options": _components(broker)}, paper_mode=True)
    assert placed is True
    assert any(o.exchange == "NFO" for o in broker.placed)
    assert quote_calls  # chain WAS fetched in paper mode
