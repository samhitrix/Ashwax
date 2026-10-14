"""
LLM intraday agent — scheduled intraday reasoning pass (src/strategies/llm_intraday/).

The MIS counterpart of src/strategies/llm_swing/. At a few fixed times of
the session the LLM reads a compact, deterministic cross-sectional snapshot of
the "in play" names (move vs previous close, VWAP / opening-range position,
relative volume, ATR, EMA slope) plus market direction, breadth and its own
closed-trade outcomes, and proposes zero or more LONG/SHORT intraday ideas.

This module ONLY produces validated ideas — it never talks to the broker or CRM.
src/orchestrator.py's `_llm_intraday_agent_loop` publishes them to
shared["llm_intraday_ideas"]; the 5-minute intraday scan loop turns a still-fresh
idea into a Signal (`_evaluate_llm_intraday_signal`) that then goes through the
normal ranked placement → `_execute_intraday_signal` → CRM G0-G10 path.

Safety-critical: symbols are restricted to the offered universe and every level
is checked against the live price snapshot. The stop-distance floor is the
explicit lesson from orb_intraday_smart (disabled 2026-09-11, PF 0.41 over 325
paper trades): a 0.95% stop sat inside normal intraday noise, so trades reliably
touched the stop and almost never reached target. A malformed or out-of-range
response degrades to "no ideas", never a guess.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, fields
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

# "intraday" (flat name, matching the pre-existing "swing" component logger.py's
# llm_swing/pipeline.py uses) -- not __name__/"src.strategies.llm_intraday.pipeline"
# any more. That dotted form nested correctly under strategies.log, but split the
# intraday engine's story across two files: this pipeline's reasoning trail in
# strategies.log, and the orchestrator's signal/TTL/cap decisions for the same
# engine in orchestrator.log. Both now share "intraday" -> intraday.log, so the
# whole engine's activity for a day reads as one file (2026-09-23).
_log = logging.getLogger("intraday")

_IST = "Asia/Kolkata"


@dataclass(frozen=True)
class ValidationLimits:
    """Code-enforced bounds on an LLM-proposed idea (all percentages of entry).

    Defaults mirror config/global.json `llm_intraday`; see that block's _note for
    the rationale behind each number."""

    max_entry_drift_pct: float = 0.5
    min_stop_pct: float = 0.5
    min_stop_atr_mult: float = 2.0
    max_stop_pct: float = 2.5
    min_reward_risk: float = 1.5
    min_target_pct: float = 0.6
    # Target must be reachable before square-off: target_pct <= reach_mult x
    # atr14_pct (5-min ATR) x sqrt(bars left). Range grows with sqrt(time), so a late
    # idea with a near target passes and a late idea needing a big move does not.
    # 0 = check off.
    reach_mult: float = 0.0
    # Directions the validator accepts. Not a float, so from_cfg handles it separately.
    allowed_directions: tuple[str, ...] = ("LONG", "SHORT")

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> ValidationLimits:
        names = {f.name for f in fields(cls)} - {"allowed_directions"}
        kwargs: dict[str, Any] = {k: float(v) for k, v in cfg.items() if k in names}
        if cfg.get("allowed_directions"):
            kwargs["allowed_directions"] = tuple(str(d).upper() for d in cfg["allowed_directions"])
        return cls(**kwargs)


@dataclass
class IntradayIdea:
    symbol: str
    direction: str  # "LONG" | "SHORT"
    entry_price: float
    stop_loss: float
    target: float
    confidence: float
    reasoning: str
    # Diagnostic only, never gates anything (reach_mult stays whatever config sets,
    # currently 0.0/off): target_pct / (atr5_pct x sqrt(minutes_left/5)) at accept
    # time, i.e. how big a move this idea needs relative to what the instrument has
    # plausibly delivered in comparable time. 2026-09-23: 3 trades that all died at
    # MIS_FORCE_EXIT (TATACHEM/HOMEFIRST/AEGISLOG) needed a manual DB replay to show
    # their targets were 3.5-4.7% moves against a 0.225% median 5m ATR. Recording
    # this on every accepted idea means the next occurrence is visible in the
    # journal without re-deriving it, and lets reach_mult be re-evaluated on this
    # agent's OWN forward trades once it reaches its pre-agreed 40-trade sample
    # (see config/global.json llm_intraday._reach_mult_note) instead of a guess.
    reach_ratio: float | None = None


@dataclass
class IntradayRunResult:
    """One reasoning pass.

    `decision` distinguishes outcomes that all used to be recorded as "no_ideas":
      proposed              - at least one idea survived validation
      rejected_by_validation- the model proposed trades, all were killed (see rejected_ideas)
      no_ideas              - the model looked and genuinely stood aside
      llm_failed            - the call errored on every attempt
      unusable_reply        - a reply arrived but never parsed as a JSON object
    The last two are failures of ours, not judgements of the market, and must never
    be read back as the agent having declined to trade.
    """

    market_context: dict[str, Any]
    thesis_text: str | None
    ideas: list[IntradayIdea] = field(default_factory=list)
    decision: str = "no_ideas"
    llm_model_used: str | None = None
    raw_llm_response: str | None = None
    rejected_ideas: list[dict[str, Any]] = field(default_factory=list)


def minutes_to_flat(now: datetime) -> float:
    """Minutes from `now` until the MIS flat-by deadline (config/market_hours.json)."""
    from src.market import sessions as _sessions

    local = now.astimezone(ZoneInfo(_IST)) if now.tzinfo else now
    flat = _sessions.minutes(_sessions.mis_flat_by())
    return max(0.0, flat - (local.hour * 60 + local.minute + local.second / 60.0))


def reachable_target_pct(atr5_pct: float, minutes_left: float, reach_mult: float) -> float:
    """Largest target distance (% of entry) plausibly reachable before square-off."""
    return reach_mult * atr5_pct * math.sqrt(max(0.0, minutes_left) / 5.0)


def _finite(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _rsi14(df: pd.DataFrame) -> float | None:
    """5-minute RSI(14) of the latest bar. The shared indicator returns NaN when the recent
    window has no losing (or no winning) closes (0/0), which is exactly a stock in a one-way
    run -- the strongest movers would have shown NO RSI. Those cases are RSI 100 / 0."""
    from src.strategies.indicators.library import compute

    val = _finite(compute(df, "RSI", period=14).iloc[-1])
    if val is not None:
        return val
    recent = df["close"].diff().tail(14).dropna()
    if recent.empty:
        return None
    if (recent >= 0).all() and (recent > 0).any():
        return 100.0
    if (recent <= 0).all() and (recent < 0).any():
        return 0.0
    return None


def build_symbol_features(df: pd.DataFrame, now: datetime) -> dict[str, Any] | None:
    """Compact intraday snapshot for one symbol from its multi-day 5m df.

    Needs at least 3 of today's bars and one prior-session bar (for previous
    close). Returns None otherwise — a symbol without enough data is simply not
    offered to the LLM."""
    from src.strategies.indicators.library import compute

    if df is None or len(df) < 3:
        return None
    idx_ist = df.index.tz_convert(_IST)
    today = now.date()
    day_mask = idx_ist.date == today
    day = df[day_mask]
    prior = df[idx_ist.date < today]
    if len(day) < 3 or prior.empty:
        return None

    prev_close = float(prior["close"].iloc[-1])
    ltp = float(day["close"].iloc[-1])
    day_open = float(day["open"].iloc[0])
    if prev_close <= 0 or ltp <= 0:
        return None

    # Opening range = the first 15 minutes (09:15-09:30 bars).
    day_idx = idx_ist[day_mask]
    or_bars = day[(day_idx.hour == 9) & (day_idx.minute < 30)]
    if or_bars.empty:
        or_bars = day.iloc[:1]
    or_high = float(or_bars["high"].max())
    or_low = float(or_bars["low"].min())
    if ltp > or_high:
        or_position = "above"
    elif ltp < or_low:
        or_position = "below"
    else:
        or_position = "inside"

    # Session VWAP only — slicing to today's bars keeps prior sessions out of it.
    vwap_val = _finite(compute(day, "VWAP").iloc[-1])
    atr_val = _finite(compute(df, "ATR", period=14).iloc[-1])
    vol_ratio = _finite(compute(df, "VOLUME_RATIO", period=20).iloc[-1])
    ema20 = compute(df, "EMA", period=20)
    ema_slope = "flat"
    if len(ema20) >= 4:
        delta = float(ema20.iloc[-1]) - float(ema20.iloc[-4])
        if abs(delta) / ltp > 0.0005:
            ema_slope = "up" if delta > 0 else "down"

    day_high = float(day["high"].max())
    day_low = float(day["low"].min())
    rsi_val = _rsi14(df)
    return {
        "ltp": round(ltp, 2),
        "chg_pct": round((ltp / prev_close - 1.0) * 100.0, 2),
        "gap_pct": round((day_open / prev_close - 1.0) * 100.0, 2),
        "vwap_dist_pct": (
            round((ltp / vwap_val - 1.0) * 100.0, 2) if vwap_val and vwap_val > 0 else None
        ),
        "or_high": round(or_high, 2),
        "or_low": round(or_low, 2),
        "or_position": or_position,
        "day_high": round(day_high, 2),
        "day_low": round(day_low, 2),
        "day_range_pct": round((day_high - day_low) / prev_close * 100.0, 2),
        # Room left to the day's extremes. A SHORT with off_day_low_pct near 0 is selling the low;
        # a LONG with off_day_high_pct near 0 is buying the high (AXISBANK 2026-09-24: sold 0.4%
        # above the day low, 92% into the day's drop, and the trade was stopped out on the bounce).
        "off_day_low_pct": round((ltp - day_low) / ltp * 100.0, 2),
        "off_day_high_pct": round((day_high - ltp) / ltp * 100.0, 2),
        "vol_ratio20": round(vol_ratio, 2) if vol_ratio is not None else None,
        "atr14_pct": round(atr_val / ltp * 100.0, 3) if atr_val is not None else None,
        "ema20_slope": ema_slope,
        # 5-minute RSI(14): computed and RECORDED with each idea for later validation, but
        # NOT shown to the LLM. On 259 closed MIS trades (2026-09-24) high-RSI entries were the
        # best bucket (33% win vs 24%) -- no evidence it helps, so it stays out of the prompt.
        "rsi14": round(rsi_val, 1) if rsi_val is not None else None,
    }


def add_relative_strength(features: dict[str, dict[str, Any]], nifty_chg_pct: float | None) -> None:
    """Add `rs_vs_nifty_pct` = the stock's % change today minus the Nifty's, in place.

    Positive = outperforming the index. In a -1.2% tape a -1.0% stock is OUTperforming and a
    -2.5% stock is lagging -- the model was only given the raw move and the index move
    separately and had to do this subtraction itself. Omitted (never faked) when the index
    move is unknown."""
    if nifty_chg_pct is None:
        return
    for f in features.values():
        if f.get("chg_pct") is not None:
            f["rs_vs_nifty_pct"] = round(float(f["chg_pct"]) - float(nifty_chg_pct), 2)


async def add_daily_trend(features: dict[str, dict[str, Any]], ohlcv_repo: Any) -> None:
    """Add `vs_50dma_pct`/`vs_200dma_pct` = the stock's live price vs its own 50/200-day
    moving average, in place. Mirrors the DMA numbers block_counter_trend_shorts computes
    downstream at execution time (orchestrator._execute_intraday_signal) -- the LLM used to
    never see them, so it kept proposing SHORTs on stocks still in a daily uptrend (e.g.
    JYOTICNC, refused 7x on 2026-10-08) that the executor was always going to refuse.

    Only covers the menu-sized candidate set (~10 symbols after select_menu), not the full
    scanned universe, so this is one extra daily-bar fetch per candidate, not per universe.
    Fail-open: a symbol with <200 daily bars or no repo is left without the fields rather
    than guessed -- the downstream DMA block itself already fails open the same way."""
    if ohlcv_repo is None:
        return
    for sym, f in features.items():
        ltp = f.get("ltp")
        if ltp is None:
            continue
        try:
            df = await ohlcv_repo.fetch_dataframe(sym, "1d", 220)
        except Exception:
            continue
        if df is None or len(df) < 200:
            continue
        sma50 = float(df["close"].rolling(50).mean().iloc[-1])
        sma200 = float(df["close"].rolling(200).mean().iloc[-1])
        if sma50 > 0:
            f["vs_50dma_pct"] = round((float(ltp) - sma50) / sma50 * 100.0, 2)
        if sma200 > 0:
            f["vs_200dma_pct"] = round((float(ltp) - sma200) / sma200 * 100.0, 2)


def quick_setup_score(df: pd.DataFrame, now: datetime) -> float | None:
    """|chg% vs previous close| x vol_ratio20 for the latest bar — the same quantity
    select_in_play ranks on, computed from the 5m df the intraday scan already holds
    (no extra DB fetch). None when there is no prior session / no volume history."""
    from src.strategies.indicators.library import compute

    if df is None or len(df) < 20:
        return None
    idx_ist = df.index.tz_convert(_IST)
    day_mask = idx_ist.date == now.date()
    prior = df[idx_ist.date < now.date()]
    if not day_mask.any() or prior.empty:
        return None
    prev_close = float(prior["close"].iloc[-1])
    ltp = float(df["close"].iloc[-1])
    if prev_close <= 0 or ltp <= 0:
        return None
    vol_ratio = _finite(compute(df, "VOLUME_RATIO", period=20).iloc[-1]) or 1.0
    return round(abs(ltp / prev_close - 1.0) * 100.0 * vol_ratio, 2)


@dataclass
class TriggerState:
    """Per-day bookkeeping for the signal-driven pass trigger."""

    day: str = ""
    last_pass_ts: float | None = None
    passes_today: int = 0
    offered: dict[str, float] = field(default_factory=dict)


def evaluate_trigger(
    state: TriggerState,
    cfg: dict[str, Any],
    now: datetime,
    *,
    first_pass: time,
    last_pass: time,
    scores: dict[str, tuple[float, float]],
    traded_today: set[str],
    trades_today: int,
    max_trades: int,
    tokens_left: int | None,
    entry_window_open: bool = True,
) -> tuple[bool, str, list[str]]:
    """Decide whether llm_intraday should ask the LLM for ideas NOW.

    Returns (fire, reason, fresh_symbols). `scores` maps bare symbol -> (setup score,
    unix ts it was computed). Pure — no I/O — so the rules are unit-testable.
    Fires a BASELINE pass at first_pass (so an opening view always exists), then a SIGNAL
    pass when a not-recently-offered symbol scores >= min_setup_score, subject to the gap /
    daily-cap / token / trade-cap guards. Never before first_pass or after last_pass."""
    _log.debug(
        "evaluate_trigger_received now=%s passes_today=%d trades_today=%d max_trades=%d "
        "scores=%d tokens_left=%s",
        now,
        state.passes_today,
        trades_today,
        max_trades,
        len(scores),
        tokens_left,
    )

    def _decide() -> tuple[bool, str, list[str]]:
        t = now.time()
        if t < first_pass:
            return False, "before_first_pass", []
        if t > last_pass:
            return False, "after_last_pass", []
        if not entry_window_open:
            # CRM gate G3 (time-of-day) would refuse any entry right now, so a call can only
            # produce ideas that die at G3 -- 2026-09-24 12:58: an LLM call plus a G9 call
            # spent on two ideas that passed every check and were then refused by G3.
            # The caller asks G3 itself (TimeFilter), so this cannot drift from the gate.
            return False, "entry_window_closed", []
        if trades_today >= max_trades:
            return False, "trade_cap_reached", []
        if state.passes_today >= int(cfg.get("max_passes_per_day", 8)):
            return False, "pass_cap_reached", []
        if tokens_left is not None and tokens_left < int(cfg.get("min_tokens_to_fire", 8000)):
            return False, "token_budget_low", []
        min_gap_s = float(cfg.get("min_gap_minutes", 30)) * 60.0
        if state.last_pass_ts is not None and now.timestamp() - state.last_pass_ts < min_gap_s:
            return False, "min_gap", []
        now_ts = now.timestamp()
        max_age = float(cfg.get("features_max_age_seconds", 900))
        re_offer_s = float(cfg.get("re_offer_minutes", 60)) * 60.0
        thr = float(cfg.get("min_setup_score", 30))
        fresh = sorted(
            (
                sym
                for sym, (score, ts) in scores.items()
                if score >= thr
                and now_ts - ts <= max_age
                and sym not in traded_today
                and now_ts - state.offered.get(sym, 0.0) >= re_offer_s
            ),
            key=lambda sym: -scores[sym][0],
        )
        if state.passes_today == 0:
            return True, "baseline", fresh
        if fresh:
            return True, "signal", fresh
        return False, "no_new_setup", []

    fire, reason, fresh_symbols = _decide()
    _log.debug(
        "evaluate_trigger_found fire=%s reason=%s fresh_symbols=%s", fire, reason, fresh_symbols
    )
    return fire, reason, fresh_symbols


def select_in_play(
    features: dict[str, dict[str, Any]], n: int, min_price: float = 100.0
) -> list[str]:
    """Deterministic pre-filter: the n names moving most on the most volume
    (|chg_pct| × vol_ratio20). Keeps the prompt small and the LLM focused on
    names where an intraday move can plausibly clear costs."""
    scored: list[tuple[float, str]] = []
    for sym, f in features.items():
        if float(f.get("ltp", 0.0)) < min_price:
            continue
        vol_ratio = f.get("vol_ratio20") or 1.0
        scored.append((abs(float(f.get("chg_pct", 0.0))) * float(vol_ratio), sym))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [sym for _, sym in scored[: max(0, n)]]


def select_menu(
    features: dict[str, dict[str, Any]],
    *,
    n: int,
    min_price: float,
    adv_cr: dict[str, float],
    min_adv_cr: float,
    offered: dict[str, float],
    now_ts: float,
    re_offer_s: float,
) -> list[str]:
    """The stocks the LLM is shown this pass: liquid, not shown recently, ranked by activity.

    Replaces select_in_play for the live pass. That ranked ALL stocks by |chg%| x volume-ratio
    with only a price floor, recomputed from scratch each pass: thin stocks (BIKAJI Rs23cr/day,
    SAREGAMA Rs19cr, SIGNATURE Rs20cr) spike the ratio easily and refilled the menu again and again
    (41% of each pass repeated the previous one; 83 of the 497 stocks trade under Rs20cr/day).
      * price >= min_price
      * average daily traded value >= min_adv_cr (Rs crore); unknown liquidity is EXCLUDED, and
        min_adv_cr <= 0 disables the floor
      * not shown to the LLM in the last re_offer_s seconds
    Returns at most n symbols, best activity score first. Never padded with repeats: an empty list
    means "nothing new worth showing", and the caller skips the LLM call."""
    scored: list[tuple[float, str]] = []
    for sym, f in features.items():
        if float(f.get("ltp", 0.0)) < min_price:
            continue
        if min_adv_cr > 0 and float(adv_cr.get(sym, 0.0)) < min_adv_cr:
            continue
        if now_ts - float(offered.get(sym, 0.0)) < re_offer_s:
            continue
        vol_ratio = f.get("vol_ratio20") or 1.0
        scored.append((abs(float(f.get("chg_pct", 0.0))) * float(vol_ratio), sym))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [sym for _, sym in scored[: max(0, n)]]


def build_market_context(
    *,
    nifty_chg_pct: float | None,
    intraday_regime: str,
    daily_regime: str,
    bearish_pct: float | None,
    vix: float,
    sector_status: dict[str, str],
    features: dict[str, dict[str, Any]],
    universe: list[str],
    news: dict[str, str] | None = None,
    minutes_to_flat: float | None = None,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "minutes_to_flat": round(minutes_to_flat) if minutes_to_flat is not None else None,
        "nifty_chg_pct": nifty_chg_pct,
        "intraday_regime": intraday_regime,
        "daily_regime": daily_regime,
        "breadth_bearish_pct": round(bearish_pct * 100.0, 1) if bearish_pct is not None else None,
        "vix": vix,
        "leading_sectors": [s for s, st in sector_status.items() if st == "LEADING"],
        "declining_sectors": [s for s, st in sector_status.items() if st == "DECLINING"],
        "candidate_universe": [{"symbol": s, **features[s]} for s in universe if s in features],
    }
    if news:
        ctx["news"] = {s: news[s] for s in universe if s in news}
    return ctx


def build_system_prompt(limits: ValidationLimits) -> str:
    reach = (
        f"Target distance must also be at most {limits.reach_mult} x range_left. "
        if limits.reach_mult > 0
        else ""
    )
    return (
        "You are a professional NSE intraday trader making ONE decision: is there a clear, "
        "high-quality trade to take right now? The default answer is PASS -- most passes should "
        "have no trade. Your reply is read by code, not a person; trades run in PAPER mode and "
        "are force-flattened at 15:10 IST; round-trip costs are about 0.10%.\n"
        "You get a MARKET line and one row per stock (columns are named on the COLUMNS line). "
        "Take a trade only if ALL of these hold:\n"
        "1. Room, not chasing: the move must not be exhausted. A SHORT needs room below "
        "(off_day_low_pct clearly above 0.5), a LONG needs room above (off_day_high_pct). A "
        "stock already down or up several percent (chg_pct) and sitting on its day extreme has "
        "already spent its move -- pass.\n"
        "2. Relative strength agrees: LONG with positive rs_vs_nifty_pct, SHORT with negative "
        "(a stock falling less than a falling index is strong, not a short).\n"
        "3. Volume confirms (vol_ratio20 well above 1) and price is on the right side of VWAP "
        "and of the opening range.\n"
        "4. Reachable: target distance at most range_left (the typical remaining move); stop at "
        f"least max({limits.min_stop_pct}%, {limits.min_stop_atr_mult} x atr14_pct) and at most "
        f"{limits.max_stop_pct}%; target at least {limits.min_target_pct}% and at least "
        f"{limits.min_reward_risk}x the stop distance. {reach}\n"
        f"5. Entry within {limits.max_entry_drift_pct}% of the stock's ltp; use ONLY listed symbols.\n"
        "6. Don't fight the daily trend on SHORTs: never propose SHORT on a stock whose "
        "vs_50dma_pct AND vs_200dma_pct are both positive (it is trading above its own 50- "
        "and 200-day average -- a confirmed uptrend, not a downtrend, regardless of today's "
        "move). Such ideas are rejected downstream every time -- propose a different symbol "
        "or PASS instead. (A LONG against the daily trend -- both vs_50dma_pct and "
        "vs_200dma_pct negative -- is allowed, just automatically sized smaller; no need to "
        "avoid it on that basis alone.)\n"
        + (
            f"7. Only {'/'.join(limits.allowed_directions)} ideas are accepted; others are discarded.\n"
            if len(limits.allowed_directions) < 2
            else ""
        )
        + 'Reply with ONLY this JSON: {"decision": "TRADE"|"PASS", "thesis": "<one sentence>", '
        '"ideas": [{"symbol": "...", "direction": "LONG"|"SHORT", "entry_price": <float>, '
        '"stop_loss": <float>, "target": <float>, "confidence": <0..1>, '
        '"reasoning": "<one sentence>"}]}. ideas must be empty when the decision is PASS; '
        "at most two ideas."
    )


def _summarise_outcomes(rows: list[Any]) -> str:
    if not rows:
        return "(no closed trades yet)"
    rs = [float(r["r"]) for r in rows if r["r"] is not None]
    wins = sum(1 for r in rs if r > 0)
    header = (
        f"  last {len(rows)} trades: wins={wins}/{len(rs)} avg_R={sum(rs) / len(rs):+.2f}"
        if rs
        else f"  last {len(rows)} trades"
    )
    lines = [header]
    for r in rows:
        r_txt = f"{float(r['r']):+.2f}R" if r["r"] is not None else "?R"
        lines.append(
            f"  {r['entry_date']} {r['symbol']} {str(r['direction']).upper()} "
            f"{r_txt} exit={r['exit_reason']}"
        )
    return "\n".join(lines)


def _summarise_today(rows: list[Any]) -> str:
    if not rows:
        return "(first pass today)"
    lines = []
    for row in rows:
        ideas = row["ideas_json"] or []
        if isinstance(ideas, str):  # asyncpg returns JSONB as text unless a codec is set
            ideas = json.loads(ideas)
        syms = ",".join(f"{i.get('symbol')}:{i.get('direction')}" for i in ideas) or "-"
        # Showing WHY earlier ideas were killed is the only way this agent can correct
        # itself within the day: without it, a pass that lost both its ideas to
        # reward:risk read back as "decision=no_ideas" and the next pass proposed the
        # same too-small targets again.
        rejected = _row_rejections(row)
        rej = (
            " rejected="
            + ",".join(
                f"{r.get('symbol')}:{r.get('reason')}"
                + (
                    f"({r.get('reward_risk')}<{r.get('min_reward_risk')})"
                    if r.get("reason") == "reward_risk_too_low"
                    else ""
                )
                for r in rejected
            )
            if rejected
            else ""
        )
        lines.append(
            f"  {row['run_ts']:%H:%M} decision={row['decision']} ideas={syms}{rej} "
            f"thesis={(row['thesis_text'] or '')[:120]!r}"
        )
    return "\n".join(lines)


def _row_rejections(row: Any) -> list[dict[str, Any]]:
    """rejected_json from a journal row; tolerates rows written before migration 078."""
    try:
        raw = row["rejected_json"]
    except (KeyError, TypeError, IndexError):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
    return [r for r in (raw or []) if isinstance(r, dict)]


_COLUMNS = (
    "symbol|ltp|chg%|gap%|vwap%|OR|off_day_low_pct|off_day_high_pct|vol_ratio20|atr14_pct|"
    "rs_vs_nifty_pct|range_left|vs_50dma_pct|vs_200dma_pct"
)


def _stock_row(sym: str, f: dict[str, Any], minutes_to_flat: float | None) -> str:
    """One short line per stock. range_left = the typical remaining move to 15:10, in %
    (5-minute ATR% x sqrt(bars left)) -- computed here so the model does not have to."""
    atr = f.get("atr14_pct")
    range_left = (
        round(float(atr) * (max(float(minutes_to_flat), 0.0) / 5.0) ** 0.5, 2)
        if atr is not None and minutes_to_flat is not None
        else "-"
    )

    def v(key: str) -> Any:
        x = f.get(key)
        return "-" if x is None else x

    return (
        f"{sym}|{v('ltp')}|{v('chg_pct')}|{v('gap_pct')}|{v('vwap_dist_pct')}|{v('or_position')}|"
        f"{v('off_day_low_pct')}|{v('off_day_high_pct')}|{v('vol_ratio20')}|{v('atr14_pct')}|"
        f"{v('rs_vs_nifty_pct')}|{range_left}|{v('vs_50dma_pct')}|{v('vs_200dma_pct')}"
    )


def build_prompt(
    context: dict[str, Any], recent_outcomes: list[Any], todays_runs: list[Any]
) -> str:
    """A short, decision-focused prompt: one market line + a compact table (was a ~7.5k-char JSON
    blob of 25 stocks with 14+ fields each, 2026-09-28)."""
    mtf = context.get("minutes_to_flat")
    ctx = context
    market = (
        f"MARKET: nifty {ctx.get('nifty_chg_pct')}% | intraday regime {ctx.get('intraday_regime')} "
        f"(daily {ctx.get('daily_regime')}) | breadth {ctx.get('breadth_bearish_pct')}% bearish | "
        f"VIX {ctx.get('vix')} | {mtf} min to flat | leading sectors "
        f"{','.join(ctx.get('leading_sectors') or []) or '-'}"
    )
    rows = [
        _stock_row(s["symbol"], s, mtf) for s in ctx.get("candidate_universe", []) if "symbol" in s
    ]
    news = ctx.get("news") or {}
    parts = [
        market,
        "COLUMNS: " + _COLUMNS,
        *rows,
    ]
    if news:
        parts += ["NEWS:", *[f"{k}: {v}" for k, v in news.items()]]
    parts += [
        "",
        "YOUR_RECENT_CLOSED_TRADES (R = realised P&L / initial risk):",
        _summarise_outcomes(recent_outcomes[:5]),
        "",
        "YOUR_EARLIER_PASSES_TODAY:",
        _summarise_today(todays_runs),
        "",
        "Decision (JSON only):",
    ]
    return "\n".join(parts)


def _validate_idea(
    raw: dict[str, Any],
    features: dict[str, dict[str, Any]],
    limits: ValidationLimits,
    minutes_left: float | None = None,
    rejected: list[dict[str, Any]] | None = None,
) -> IntradayIdea | None:
    """`rejected`, when given, collects one record per killed idea.

    Rejections used to exist only as a log line, so a pass where the model
    proposed real trades that validation killed was journalled — and replayed to
    the model next pass — as `no_ideas`. It then had no way to know its targets
    were being rejected as too small, and kept proposing the same shape.
    """
    try:
        symbol = str(raw["symbol"]).strip()
        direction = str(raw["direction"]).strip().upper()
        entry = float(raw["entry_price"])
        sl = float(raw["stop_loss"])
        target = float(raw["target"])
        confidence = float(raw.get("confidence", 0.5))
        reasoning = str(raw.get("reasoning", ""))[:400]
    except (KeyError, TypeError, ValueError) as exc:
        _log.warning("intraday_idea_malformed err=%s raw=%r", exc, raw)
        if rejected is not None:
            rejected.append({"symbol": str(raw.get("symbol", "?")), "reason": "malformed"})
        return None

    def _reject(reason: str, **detail: Any) -> None:
        _log.warning(
            "intraday_idea_rejected reason=%s symbol=%s dir=%s entry=%s sl=%s target=%s%s",
            reason,
            symbol,
            direction,
            entry,
            sl,
            target,
            "".join(f" {k}={v}" for k, v in detail.items()),
        )
        if rejected is not None:
            rejected.append(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "entry_price": entry,
                    "stop_loss": sl,
                    "target": target,
                    "reason": reason,
                    **detail,
                }
            )

    direction = {"BUY": "LONG", "SELL": "SHORT"}.get(direction, direction)
    if direction not in ("LONG", "SHORT"):
        _reject("bad_direction")
        return None
    if direction not in limits.allowed_directions:
        _reject("direction_not_allowed", allowed=list(limits.allowed_directions))
        return None
    feat = features.get(symbol)
    if feat is None:
        _reject("unknown_symbol")
        return None
    anchor = float(feat["ltp"])
    if anchor <= 0 or abs(entry - anchor) / anchor * 100.0 > limits.max_entry_drift_pct:
        _reject(
            "price_drift",
            ltp=anchor,
            drift_pct=round(abs(entry - anchor) / anchor * 100.0, 2) if anchor > 0 else None,
            max_drift_pct=limits.max_entry_drift_pct,
        )
        return None
    ordered = (0.0 < sl < entry < target) if direction == "LONG" else (0.0 < target < entry < sl)
    if not ordered:
        _reject("invalid_levels")
        return None

    stop_pct = abs(entry - sl) / entry * 100.0
    target_pct = abs(target - entry) / entry * 100.0
    atr_pct = feat.get("atr14_pct") or 0.0
    stop_floor = max(limits.min_stop_pct, limits.min_stop_atr_mult * float(atr_pct))
    # Compare at the 2dp precision the rejection is logged at, else "1.57 < 1.57" rejects.
    if round(stop_pct, 2) < round(stop_floor, 2):
        _reject("stop_too_tight", stop_pct=round(stop_pct, 2), floor_pct=round(stop_floor, 2))
        return None
    if stop_pct > limits.max_stop_pct:
        _reject("stop_too_wide", stop_pct=round(stop_pct, 2), max_pct=limits.max_stop_pct)
        return None
    if target_pct < limits.min_target_pct:
        _reject("target_too_small", target_pct=round(target_pct, 2), min_pct=limits.min_target_pct)
        return None
    reward_risk = round(target_pct / stop_pct, 2)
    if reward_risk < limits.min_reward_risk:
        _reject(
            "reward_risk_too_low",
            reward_risk=reward_risk,
            min_reward_risk=limits.min_reward_risk,
        )
        return None
    if limits.reach_mult > 0 and minutes_left is not None and float(atr_pct) > 0:
        reach = reachable_target_pct(float(atr_pct), minutes_left, limits.reach_mult)
        if target_pct > reach:
            _reject(
                "target_unreachable",
                target_pct=round(target_pct, 2),
                reachable_pct=round(reach, 2),
                minutes_left=round(minutes_left),
                atr5_pct=round(float(atr_pct), 3),
            )
            return None

    # Passive diagnostic, computed regardless of reach_mult (which stays whatever
    # config sets — currently 0.0/off): target_pct as a multiple of atr5_pct x
    # sqrt(minutes_left/5), i.e. reachable_target_pct() with reach_mult pinned to
    # 1. A value near/above the reach_mult thresholds already measured elsewhere
    # (p50 0.92, p70 1.30, p80 1.62 on this agent's own universe) flags a target
    # that needs an outsized move, WITHOUT rejecting the idea.
    reach_ratio = None
    if minutes_left is not None and float(atr_pct) > 0:
        _denom = reachable_target_pct(float(atr_pct), minutes_left, 1.0)
        reach_ratio = round(target_pct / _denom, 3) if _denom > 0 else None
        if reach_ratio is not None:
            _log.debug(
                "intraday_idea_reach_ratio symbol=%s dir=%s target_pct=%.2f atr5_pct=%.3f "
                "minutes_left=%.0f reach_ratio=%.2f",
                symbol,
                direction,
                target_pct,
                float(atr_pct),
                minutes_left,
                reach_ratio,
            )

    confidence = max(0.0, min(1.0, confidence))
    return IntradayIdea(symbol, direction, entry, sl, target, confidence, reasoning, reach_ratio)


def still_valid_at(
    idea: dict[str, Any],
    price: float,
    limits: ValidationLimits,
    minutes_left: float | None = None,
) -> tuple[float, float] | None:
    """Re-apply the validation rules with `price` as the entry.

    An idea was validated against a snapshot up to idea_ttl_minutes old; entering
    at a different price changes its stop distance and reward:risk (a 0.4% drift
    on a 0.5% stop leaves a 0.1% stop — pure noise).

    stop_loss/target are shifted by the SAME distance from `price` that the LLM
    originally proposed from its own entry, rather than re-checked as frozen
    absolute levels: a genuine momentum idea is expected to keep drifting in its
    own direction, and checking reward:risk against unmoved price levels
    mechanically shrinks the reward side (and can inflate the risk side) the
    closer price gets to target -- so the more real the momentum, the more
    likely a frozen-level check kills it (2026-09-18: POONAWALLA drifted +0.25%
    toward target and its reward:risk collapsed from 1.70 to 1.15, well past
    the 1.5 floor, purely from that shift). Shifting both levels with price
    preserves the R:R a professional would actually trade -- move your stop and
    target with you, don't leave them pinned to where price was when you first
    looked. The stop-floor/ceiling and target-floor checks below still re-apply
    fresh against the shifted levels as % of the NEW price, so a large drift
    into materially different ATR/volatility territory is still caught.

    `idea` is the dict published to shared["llm_intraday_ideas"] (IntradayIdea
    fields + atr14_pct).

    Returns the shifted (stop_loss, target) to actually place the order at if
    still valid, or None if not. The caller MUST use these shifted levels, not
    the idea's original ones -- placing the order at the frozen levels after
    this function approved the shifted ones would trade the worse R:R this
    fix exists to avoid, not the one that was actually validated."""
    entry0 = float(idea["entry_price"])
    sl0 = float(idea["stop_loss"])
    target0 = float(idea["target"])
    if str(idea["direction"]).upper() == "SHORT":
        new_sl = price + (sl0 - entry0)
        new_target = price - (entry0 - target0)
    else:
        new_sl = price - (entry0 - sl0)
        new_target = price + (target0 - entry0)
    raw = {**idea, "entry_price": price, "stop_loss": new_sl, "target": new_target}
    feats = {str(idea["symbol"]): {"ltp": price, "atr14_pct": idea.get("atr14_pct")}}
    validated = _validate_idea(raw, feats, limits, minutes_left)
    if validated is None:
        return None
    return (validated.stop_loss, validated.target)


def _strip_code_fence(text: str) -> str:
    """Unwrap a model reply down to its JSON object.

    Name kept (callers/tests unchanged) but this now also survives a reply that
    puts a sentence before or after the JSON, which the fence-only version treated
    as unparseable — costing a retry and then the whole slot. Shared with the swing
    agent so the two cannot drift apart again.
    """
    from src.llm.response_parse import extract_json_object

    return extract_json_object(text)


def _parses_as_json_object(raw_text: str) -> bool:
    try:
        return isinstance(json.loads(_strip_code_fence(raw_text)), dict)
    except (json.JSONDecodeError, ValueError):
        return False


def parse_llm_response(
    raw_text: str,
    features: dict[str, dict[str, Any]],
    limits: ValidationLimits,
    minutes_left: float | None = None,
    rejected: list[dict[str, Any]] | None = None,
) -> tuple[str | None, list[IntradayIdea]]:
    """Parse + validate the LLM's JSON. Never raises — malformed output degrades
    to (None, []), i.e. no ideas this pass.

    `rejected`, when given, is filled with one record per idea validation killed,
    so the caller can tell "the model saw nothing" apart from "the model proposed
    trades we threw away".
    """
    try:
        data = json.loads(_strip_code_fence(raw_text))
        if not isinstance(data, dict):
            raise ValueError("top-level JSON is not an object")
    except (json.JSONDecodeError, ValueError) as exc:
        _log.warning("intraday_response_malformed err=%s raw=%r", exc, raw_text[:200])
        return None, []

    thesis = data.get("thesis")
    thesis = str(thesis)[:600] if thesis else None
    raw_ideas = data.get("ideas", [])
    if not isinstance(raw_ideas, list):
        return thesis, []
    ideas: list[IntradayIdea] = []
    for raw in raw_ideas:
        if isinstance(raw, dict):
            idea = _validate_idea(raw, features, limits, minutes_left, rejected)
            if idea is not None:
                ideas.append(idea)
    _log.debug(
        "intraday_parse_done offered=%d accepted=%d rejected=%s",
        len(raw_ideas),
        len(ideas),
        [(r.get("symbol"), r.get("reason")) for r in (rejected or [])],
    )
    return thesis, ideas


async def run_intraday_reasoning(
    llm_client: Any,
    market_context: dict[str, Any],
    recent_outcomes: list[Any],
    todays_runs: list[Any],
    features: dict[str, dict[str, Any]],
    limits: ValidationLimits,
    max_ideas: int,
    timeout_secs: float = 30.0,
    minutes_left: float | None = None,
) -> IntradayRunResult:
    """One reasoning pass. Never raises — any failure returns no ideas."""
    import asyncio

    _log.debug(
        "run_intraday_reasoning_received features=%d recent_outcomes=%d todays_runs=%d "
        "max_ideas=%d",
        len(features),
        len(recent_outcomes),
        len(todays_runs),
        max_ideas,
    )
    prompt = build_prompt(market_context, recent_outcomes, todays_runs)
    model_used = getattr(llm_client, "_provider", None)
    model_used = str(model_used) if model_used is not None else None
    # An error, empty reply or unparseable reply is a FAILED call, not "no ideas":
    # 2026-09-21 13:45 (the day's last slot) a Gemini 503 fell through the chain to an
    # empty reply that was recorded as a clean no_ideas pass, silently losing the
    # afternoon. Retry once; a genuine "no setup" reply is still valid JSON and is
    # accepted first time.
    raw_text = ""
    thesis: Any = None
    ideas: list[Any] = []
    rejected: list[dict[str, Any]] = []
    for attempt in (1, 2):
        # The model's hidden thinking shares the output budget, so 3000 was sometimes all consumed
        # before the visible JSON (2026-09-28: reply `{"th`). Give it real room, and on the retry
        # change the prompt and raise the limit -- never re-send the identical request.
        _prompt = (
            prompt
            if attempt == 1
            else prompt + "\n\nIMPORTANT: your previous reply was cut off or was not valid JSON. "
            "Reply with ONLY the JSON object, keep the thesis to one sentence, no other text."
        )
        _max_tokens = 6000 if attempt == 1 else 9000
        try:
            raw_text = await asyncio.wait_for(
                llm_client.complete(
                    _prompt,
                    system=build_system_prompt(limits),
                    temperature=0.3,
                    max_tokens=_max_tokens,
                ),
                timeout=timeout_secs,
            )
        except Exception as exc:
            _log.warning("intraday_llm_call_failed attempt=%d err=%s", attempt, exc)
            if attempt == 2:
                # raw_llm_response carries whatever the last attempt returned (often
                # ""), because "we have no evidence" is itself the evidence that this
                # was our failure and not the model declining to trade.
                return IntradayRunResult(
                    market_context,
                    None,
                    decision="llm_failed",
                    llm_model_used=model_used,
                    raw_llm_response=raw_text or f"<call failed: {exc}>",
                )
            continue
        used = getattr(llm_client, "last_provider", None)
        if used is not None:
            model_used = str(used)
        if _parses_as_json_object(raw_text):
            thesis, ideas = parse_llm_response(raw_text, features, limits, minutes_left, rejected)
            break
        _log.warning("intraday_llm_unusable_reply attempt=%d provider=%s", attempt, model_used)
        if attempt == 2:
            return IntradayRunResult(
                market_context,
                None,
                decision="unusable_reply",
                llm_model_used=model_used,
                raw_llm_response=raw_text,
            )

    if len(ideas) > max_ideas:
        _log.debug(
            "run_intraday_reasoning_capped kept=%d dropped=%s",
            max_ideas,
            [getattr(i, "symbol", None) for i in ideas[max_ideas:]],
        )
    ideas = ideas[: max(0, max_ideas)]
    if ideas:
        decision = "proposed"
    elif rejected:
        decision = "rejected_by_validation"
    else:
        decision = "no_ideas"
    _log.debug(
        "run_intraday_reasoning_found model=%s decision=%s ideas=%d symbols=%s rejected=%s",
        model_used,
        decision,
        len(ideas),
        [getattr(i, "symbol", None) for i in ideas],
        [(r.get("symbol"), r.get("reason")) for r in rejected],
    )
    return IntradayRunResult(
        market_context=market_context,
        thesis_text=thesis,
        ideas=ideas,
        decision=decision,
        llm_model_used=model_used,
        raw_llm_response=raw_text,
        rejected_ideas=rejected,
    )
