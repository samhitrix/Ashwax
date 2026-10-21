"""Read-only Flattrade market-data check (can Flattrade replace Kite for DATA?).

Usage:  python scripts/check_flattrade_data.py [--symbol RELIANCE] [--underlying NIFTY]

Needs FLATTRADE_API_KEY, FLATTRADE_USER_ID and today's FLATTRADE_ACCESS_TOKEN in .env
(run ``python -m src.brokers.flattrade_auth`` first). Calls ONLY read endpoints (session,
scrip master, quotes, depth, candles, option quotes, funds, positions, holdings); it never
places, modifies or cancels an order and never touches the running service. Prints one
PASS / FAIL / INFO line per check with timings, then a summary.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

_results: list[tuple[str, str]] = []


def _report(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name))
    print(f"[{status:4}] {name}" + (f" - {detail}" if detail else ""))


async def _check(name: str, fn: Callable[[], Awaitable[str]]) -> None:
    t0 = time.monotonic()
    try:
        detail = await fn()
    except Exception as exc:  # noqa: BLE001 -- a check failing is the output, not a crash
        _report("FAIL", name, f"{type(exc).__name__}: {exc}")
        return
    _report("PASS", name, f"{detail} ({time.monotonic() - t0:.1f}s)")


async def _run(symbol: str, underlying: str) -> None:
    from src.brokers.adapters.flattrade.adapter import FlattradeAdapter

    adapter = FlattradeAdapter.from_env()
    token = __import__("os").environ.get("FLATTRADE_ACCESS_TOKEN", "")
    if not token:
        raise SystemExit(
            "FLATTRADE_ACCESS_TOKEN missing - run: python -m src.brokers.flattrade_auth"
        )
    adapter.set_access_token(token)

    async def session() -> str:
        st = await adapter.get_session_status()
        if not st.is_valid:
            raise RuntimeError("token not accepted (expired? log in again after 6 AM IST)")
        return "token valid"

    await _check("session / UserDetails", session)

    async def scrip(exchange: str) -> str:
        n = len(await adapter.get_instruments(exchange))
        if n == 0:
            raise RuntimeError("empty scrip master")
        return f"{n} {exchange} instruments"

    await _check("scrip master NSE", lambda: scrip("NSE"))
    await _check("scrip master NFO", lambda: scrip("NFO"))

    async def ltp() -> str:
        out = await adapter.get_ltp([f"NSE:{symbol}"])
        v = next(iter(out.values()), 0.0)
        if v <= 0:
            raise RuntimeError(f"no price returned: {out}")
        return f"{symbol} ltp={v}"

    await _check(f"LTP {symbol}", ltp)

    async def depth() -> str:
        d = await adapter.get_depth(f"NSE:{symbol}")
        return f"bids={len(d.bids)} asks={len(d.asks)}"

    await _check(f"depth {symbol}", depth)

    async def index_ltp() -> str:
        for sym in (f"NSE:{underlying}", "NSE:NIFTY 50", underlying):
            try:
                out = await adapter.get_ltp([sym])
            except Exception:  # noqa: BLE001, S112 -- try the next spelling
                continue
            v = next(iter(out.values()), 0.0)
            if v > 0:
                return f"{sym} ltp={v}"
        raise RuntimeError(f"index {underlying} not resolvable under any spelling tried")

    await _check(f"index spot {underlying}", index_ltp)

    for interval, days in (("day", 90), ("5minute", 5), ("15minute", 10)):

        async def candles(interval: str = interval, days: int = days) -> str:
            df = await adapter.get_ohlcv(f"NSE:{symbol}", interval, days)
            if df.empty:
                raise RuntimeError("no candles")
            last = df.iloc[-1]
            return (
                f"{len(df)} bars, first={df['date'].iloc[0]:%Y-%m-%d %H:%M}, "
                f"last={df['date'].iloc[-1]:%Y-%m-%d %H:%M} close={last['close']}"
            )

        await _check(f"candles {symbol} {interval}/{days}d", candles)

    async def bar_label() -> str:
        df = await adapter.get_ohlcv(f"NSE:{symbol}", "5minute", 5)
        first_of_day = df.groupby(df["date"].dt.date)["date"].min().iloc[-1]
        return (
            f"first 5m bar of latest day stamped {first_of_day:%H:%M} "
            "(09:15 = start-of-bar, as the system assumes; 09:20 = end-of-bar -> tell Claude)"
        )

    await _check("5m bar label (start vs end)", bar_label)

    async def options() -> str:
        insts = await adapter.get_instruments("NFO")
        opts = [
            i
            for i in insts
            if i.name == underlying and i.instrument_type in ("CE", "PE") and i.expiry
        ]
        if not opts:
            raise RuntimeError(f"no {underlying} options in the NFO scrip master")
        nearest = min(
            i.expiry for i in opts if i.expiry and i.expiry.date() >= datetime.now().date()
        )
        chain = [i for i in opts if i.expiry == nearest]
        strikes = sorted({i.strike for i in chain if i.strike})
        spot = 0.0
        for sym in (f"NSE:{underlying}", "NSE:NIFTY 50"):
            try:
                spot = next(iter((await adapter.get_ltp([sym])).values()), 0.0)
            except Exception:  # noqa: BLE001, S112
                continue
            if spot > 0:
                break
        mid = min(strikes, key=lambda k: abs(k - spot)) if spot > 0 else strikes[len(strikes) // 2]
        near = [i for i in chain if i.strike in sorted(strikes, key=lambda k: abs(k - mid))[:5]]
        t0 = time.monotonic()
        quotes = await adapter.get_option_quotes([f"NFO:{i.symbol}" for i in near])
        dt = time.monotonic() - t0
        with_oi = sum(1 for q in quotes.values() if q.oi > 0)
        per_leg = dt / max(len(near), 1)
        return (
            f"expiry {nearest:%Y-%m-%d}, {len(quotes)}/{len(near)} legs quoted, {with_oi} with OI, "
            f"{per_leg:.2f}s/leg -> full chain of {len(chain)} legs ~{per_leg * len(chain):.0f}s"
        )

    await _check(f"option quotes {underlying} (nearest expiry, 5 strikes)", options)

    async def polled_ticks() -> str:
        prices = []
        for _ in range(5):
            prices.append(next(iter((await adapter.get_ltp([f"NSE:{symbol}"])).values()), 0.0))
            await asyncio.sleep(1)
        return f"polled LTP x5: {prices} (REST polling only: websocket ticks are NOT wired)"

    await _check("tick substitute (polled LTP)", polled_ticks)

    async def funds() -> str:
        m = await adapter.get_margins()
        return f"equity={m.equity} available={m.available_cash} used={m.used_margin}"

    await _check("funds / limits (read-only)", funds)

    async def positions() -> str:
        return f"{len(await adapter.get_positions())} open positions"

    await _check("positions (read-only)", positions)

    async def holdings() -> str:
        return f"{len(await adapter.get_holdings())} holdings (quantity mapping UNVERIFIED)"

    await _check("holdings (read-only)", holdings)


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=True)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="RELIANCE")
    ap.add_argument("--underlying", default="NIFTY")
    args = ap.parse_args()
    asyncio.run(_run(args.symbol, args.underlying))
    failed = [n for s, n in _results if s == "FAIL"]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    for n in failed:
        print(f"  FAILED: {n}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
