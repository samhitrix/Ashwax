"""Dry run of the weekly condor (hypothesis B) entry decision.

Real config (options_settings.json), real NIFTY chain from Kite for the target expiry,
real DB (read-only session: event calendar + once-per-expiry check), real G9 LLM
reviewer. NO ORDER IS PLACED: the executor only records. CRM is run with an
approve-all stand-in (the real CRM context is built at service boot), so the
G0-G10 verdict itself is not part of this dry run.

Usage: python scripts/research/dryrun_b_entry.py [YYYY-MM-DD] [HH:MM]
       (defaults: next trading day, 10:10 IST)
"""

from __future__ import annotations

# ruff: noqa: E402
# mypy: ignore-errors
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import asyncpg
from dotenv import dotenv_values

IST = ZoneInfo("Asia/Kolkata")


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls = []

    async def execute_structure(self, protection_legs, income_legs, signal_id):
        from src.options.options_leg_executor import ExecutionResult

        self.calls.append((protection_legs, income_legs))
        return ExecutionResult(position_id="DRYRUN", outcome="ALL_FILLED")


class _FixedChain:
    def __init__(self, chain) -> None:
        self._chain = chain

    async def fetch_tier1(self, underlying):
        return self._chain


class _ApproveAll:
    def evaluate(self, signal, ctx):
        from src.risk.crm import CRMDecision

        print(
            f"  CRM signal: name={signal.strategy_name} registry={signal.meta.get('registry_name')} "
            f"risk_rupees={signal.risk_rupees:.0f} lots={signal.intended_quantity} "
            f"llm_flag={signal.meta.get('llm_flag')} conf={signal.meta.get('llm_confidence')}"
        )
        return CRMDecision(
            signal_id=signal.signal_id,
            approved=True,
            final_quantity=signal.intended_quantity,
            gate_results=[],
        )


async def main() -> None:
    from src.brokers.adapters.kite.adapter import KiteAdapter
    from src.brokers.adapters.kite.auth import KiteAuth
    from src.db.repositories.options import OptionsRepository
    from src.llm.gate_policy import load_gate_policy
    from src.market.calendar import is_trading_day
    from src.options import seller_settings as ss
    from src.options.event_calendar import EventCalendar
    from src.options.greeks_budget import compute_budget, to_rupees
    from src.options.nfo_chain import NFOChainProvider
    from src.options.seller_desk import (
        SellerDesk,
        _per_lot_greeks,
        build_weekly_condor,
        first_trading_day_of_cycle,
    )
    from src.options.strategy_selector import StrategySelector

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("options.seller_desk").setLevel(logging.INFO)

    if len(sys.argv) > 1:
        day = date.fromisoformat(sys.argv[1])
    else:
        day = date.today() + timedelta(days=1)
        while not is_trading_day(day):
            day += timedelta(days=1)
    hhmm = sys.argv[2] if len(sys.argv) > 2 else "10:10"
    now = datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time(), IST)
    cfg = ss.weekly_condor()
    s = ss.load_settings()
    print(
        f"simulated time: {now:%a %Y-%m-%d %H:%M} IST | weekly_condor cfg: {cfg | {'_note': '...'}}"
    )

    broker = KiteAdapter(KiteAuth.from_env())
    provider = NFOChainProvider(broker)
    # The chain the desk would see at entry: nearest expiry >= 2 days from that morning.
    chain = await provider.get_chain(f"NSE:{cfg['underlying']}", min_days_to_expiry=2, now=now)
    if chain is None:
        print("NO CHAIN")
        return
    expiry = chain.expiry.date()
    print(
        f"chain: expiry={expiry} spot={chain.spot:.1f} lot_size={chain.lot_size} strikes={len(chain.strikes)}"
    )
    print(
        f"first trading day of this cycle = {first_trading_day_of_cycle(expiry)} (simulated day {day})"
    )

    built = build_weekly_condor(
        chain, 1, short_delta=float(cfg["short_delta"]), wing_points=float(cfg["wing_points"])
    )
    if built is None:
        print("structure build FAILED (strike/wing/liquidity)")
    else:
        p, i = built
        g = to_rupees(_per_lot_greeks(chain, p, i, 1), chain.spot)
        budget = compute_budget(float(s["options_capital_rupees"]), s.get("greeks_budget", {}))
        print(
            "legs:",
            [
                f"{leg.direction} {leg.strike:g}{leg.instrument_type} @{leg.premium:.2f} d={leg.delta:+.2f}"
                for leg in p + i
            ],
        )
        print(
            f"per-lot greeks (Rs): gamma={g.net_gamma:,.0f} vega={g.net_vega:,.0f} "
            f"delta={g.net_delta:,.0f} theta={g.net_theta:,.0f}"
        )
        print(
            f"budget (Rs):         gamma={budget.gamma:,.0f} vega={budget.vega:,.0f} "
            f"delta={budget.delta:,.0f} theta={budget.theta:,.0f}"
        )

    pool = await asyncpg.create_pool(
        dotenv_values(ROOT / ".env")["DB_URL"],
        min_size=1,
        max_size=2,
        server_settings={"default_transaction_read_only": "on"},
    )
    repo = OptionsRepository(pool)
    sizing = s.get("sizing", {})
    desk = SellerDesk(
        StrategySelector(),
        EventCalendar(repo),
        _RecordingExecutor(),
        _FixedChain(chain),
        options_capital=float(s["options_capital_rupees"]),
        risk_per_trade_pct=float(sizing.get("risk_per_trade_pct", 2.0)),
        margin_safety_pct=1.0
        - float(s.get("margin", {}).get("margin_cushion_minimum_pct", 20)) / 100.0,
        greeks_budget=compute_budget(
            float(s["options_capital_rupees"]), s.get("greeks_budget", {})
        ),
        min_rom_pct=float(s.get("rom", {}).get("min_annualized_pct", 24)),
        repo=repo,
        is_paper=True,
    )
    await desk.restore_positions()  # real open positions -> real greeks/margin usage
    print(
        f"open desk positions restored: {[(x.strategy_type, x.underlying) for x in desk.positions.values()]}"
    )
    required, _ = load_gate_policy()
    ctx = SimpleNamespace(
        llm_gate_required=required, strategy_execution_mode={cfg["registry_name"]: "paper"}
    )
    print("\n--- desk decision (log lines below) ---")
    pos = await desk.run_weekly_condor_cycle(
        f"NSE:{cfg['underlying']}",
        today=day,
        now=now,
        available_margin=desk.available_margin(),
        regime="UNKNOWN",
        vix=None,
        crm=_ApproveAll(),
        crm_ctx=ctx,
    )
    print(
        "\nRESULT:",
        "WOULD OPEN (no order placed)" if pos else "BLOCKED - see the 'skipped reason=' line above",
    )
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
