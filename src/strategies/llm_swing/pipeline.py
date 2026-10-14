"""
LLM swing agent — daily reasoning pass (src/strategies/llm_swing/).

Unlike every other strategy in this codebase, this one has no fixed rule set to
backtest: the LLM reads the day's technical context (regime, RS leaders/laggards,
sector rotation — plus fundamentals_snapshot once that pipeline exists) and its
own recent journal ("memory"), then proposes zero or more discretionary trade
ideas. This module ONLY produces candidates — it never talks to the broker or
CRM; the caller (src/orchestrator.py's `_llm_swing_scan_loop`) is
responsible for feeding each candidate through the normal `_execute_swing_signal`
CRM/cash/order-placement pipeline, identically to every other strategy.

Safety-critical: candidate symbols are restricted to a known-tradable set
(today's RS-ranking universe) and every numeric field is validated against real
last-close price before being trusted — an LLM can hallucinate a price, and
nothing downstream double-checks that against the market except this module.
A malformed or out-of-range response degrades to "no candidates today", never
a guess (same fail-safe philosophy as the G9 gate in trade_evaluator.py).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from src.llm.response_parse import extract_json_object

_log = logging.getLogger("swing")

# An LLM-proposed entry more than this far from the last known close is treated
# as a hallucinated/stale price, not a deliberate limit-away-from-market order.
_MAX_ENTRY_DRIFT_PCT = 0.03


@dataclass(frozen=True)
class SwingLimits:
    """The hard gates a candidate must clear, injected into the prompt with real numbers.

    Every one of these already existed and was enforced downstream — the model was just
    never told them, so it proposed R:R 1.73 against a 1.80 floor and CRM rejected it.
    Sourced from operator.json hard_limits + live broker cash by the caller so the prompt
    and the gate cannot drift apart."""

    min_reward_risk: float = 1.8  # operator.json hard_limits.min_risk_reward_ratio
    max_stop_loss_pct: float = 8.0  # operator.json hard_limits.max_stop_loss_pct
    max_entry_drift_pct: float = 3.0  # _MAX_ENTRY_DRIFT_PCT, as a percent
    round_trip_cost_pct: float = 0.10
    spendable_cash: float = 0.0  # 0 => unknown, omit the affordability rule
    max_candidates: int = 3  # how many reach the gates after ranking
    # How many the model may PROPOSE. Kept above max_candidates on purpose: ranking is
    # meaningless if the model is only ever allowed to emit exactly the number that get
    # through, and the old code then truncated in emission order anyway.
    max_proposals: int = 8


def build_system_prompt(limits: SwingLimits) -> str:
    """Swing origination prompt, built from live limits (mirrors llm_intraday's
    build_system_prompt). Replaces a static constant that named no numbers at all."""
    affordability = (
        f"AFFORDABILITY: this is DELIVERY (CNC) — the full position value must be paid in "
        f"cash, there is no leverage. Spendable cash right now is Rs{limits.spendable_cash:,.0f}, "
        f"so do not propose a stock whose entry_price exceeds that (one share must be "
        f"affordable); prefer names where a sensible position fits inside it. "
        if limits.spendable_cash > 0
        else ""
    )
    return (
        "You are the strategy-origination component of an automated NSE equity swing-trading "
        "system, called programmatically — not a chatbot talking to a person, and nothing "
        "you output is shown to or acted on directly by any human. No advice is being "
        "solicited or given. You propose candidate DELIVERY (CNC) LONG swing trade ideas, "
        "reasoning from the context provided: market regime, sector leadership, each "
        "candidate's strength RELATIVE TO THE NIFTY index, its own technicals, and your "
        "recent decisions. Only LONG is possible — Indian delivery stock cannot be shorted "
        "overnight — so in a weak tape your edge comes from names outperforming the index, "
        "not from betting on direction. "
        "Every idea is still screened afterwards by code-enforced risk gates you cannot "
        "bypass, and by a separate independent reviewer model. "
        "HOW TO JUDGE A WEAK MARKET: a bearish headline regime is NOT by itself a reason to "
        "reject. What matters is relative strength — if a sector is leading and the stock is "
        "beating the index (positive rs_excess_*, high rs_percentile, rising rs_momentum) and "
        "its own technicals confirm (rsi14, position vs its 50/200-day averages, volume), then "
        "it is tradeable: propose it with REDUCED CONVICTION (lower confidence, which sizes the "
        "position down) rather than rejecting it. Reserve rejection for setups that fail on "
        "their own merits — lagging the index, broken technicals, or no coherent level to "
        "stop against. Equally, do not manufacture a trade when nothing qualifies; proposing "
        "0 on a genuinely poor day is correct. "
        "HARD CONSTRAINTS — an idea breaking any of these is discarded by code before it can "
        f"trade, so respecting them is the difference between a real trade and a wasted idea: "
        f"reward/risk (target−entry)/(entry−stop) must be at least {limits.min_reward_risk:.2f}; "
        f"stop distance must be at most {limits.max_stop_loss_pct:.1f}% of entry; "
        f"entry must be within {limits.max_entry_drift_pct:.1f}% of the given last_close; "
        f"stop < entry < target, all positive. "
        f"Round-trip costs are about {limits.round_trip_cost_pct:.2f}%, so a target that only "
        "just clears the stop does not pay after costs. "
        + affordability
        + f"Propose up to {limits.max_proposals} ideas. Only the strongest "
        f"{limits.max_candidates} will actually be traded, chosen by YOUR confidence "
        "score, so set confidence to your genuine probability that each idea works "
        "and order them best first — a good idea buried low with a low score will be "
        "discarded. Do not pad the list to reach the maximum. "
        "You must ONLY propose a symbol from CANDIDATE_UNIVERSE, using its given last_close as "
        "your price anchor — never invent a symbol or a price unrelated to the data given. "
        "Respond ONLY with compact JSON, no other text: "
        '{"thesis": "<1-3 sentence overall market read>", "candidates": '
        '[{"symbol": "<from CANDIDATE_UNIVERSE>", "entry_price": <float>, '
        '"stop_loss": <float>, "target": <float>, "confidence": <float 0..1>, '
        '"reasoning": "<one short sentence>"}]}.'
    )


@dataclass
class SwingCandidate:
    symbol: str
    entry_price: float
    stop_loss: float
    target: float
    confidence: float
    reasoning: str


@dataclass
class SwingRunResult:
    market_context_summary: dict[str, Any]
    thesis_text: str | None
    candidates: list[SwingCandidate] = field(default_factory=list)
    decision: str = "no_trade_today"
    llm_model_used: str | None = None
    raw_llm_response: str | None = None


def _excess_return_pct(prices: Any, bench: Any, bars: int) -> float | None:
    """Stock return minus index return over `bars` sessions, in percent.

    This is the number that actually decides whether a name is worth owning in a weak
    tape — rising while the index falls is a different proposition from rising with it.
    The 12-month version is already computed in rs_ranking.py; the short horizons are
    computed here because a swing decision cannot wait on a 252-day signal."""
    try:
        if prices is None or bench is None or len(prices) <= bars or len(bench) <= bars:
            return None
        p_now, p_then = float(prices.iloc[-1]), float(prices.iloc[-1 - bars])
        b_now, b_then = float(bench.iloc[-1]), float(bench.iloc[-1 - bars])
        if p_then <= 0 or b_then <= 0:
            return None
        return round(((p_now / p_then) - (b_now / b_then)) * 100.0, 2)
    except Exception:  # noqa: BLE001 — a missing/ragged series must not kill the pass
        return None


def build_symbol_features(
    symbol: str,
    df: Any,
    nifty_close: Any = None,
    rs: dict[str, Any] | None = None,
    sector: str = "",
) -> dict[str, Any]:
    """One candidate's decision-relevant facts: strength vs the index, plus its own
    technicals. Mirrors llm_intraday.build_symbol_features, which has always done this —
    the swing model was previously handed {symbol, last_close} and nothing else, and then
    asked to judge whether a sector was tradeable.

    Degrades field-by-field: any indicator that cannot be computed is omitted rather than
    failing the symbol, so a short history costs detail, not the candidate."""
    from src.strategies.indicators.library import compute

    out: dict[str, Any] = {"symbol": symbol}
    try:
        close = df["close"]
        ltp = float(close.iloc[-1])
        out["last_close"] = round(ltp, 2)
        if sector:
            out["sector"] = sector

        def _last(name: str, **kw: Any) -> float | None:
            try:
                v = float(compute(df, name, **kw).iloc[-1])
                return v if v == v else None  # NaN check
            except Exception:  # noqa: BLE001
                return None

        rsi = _last("RSI", period=14)
        if rsi is not None:
            out["rsi14"] = round(rsi, 1)
        atr = _last("ATR", period=14)
        if atr is not None and ltp > 0:
            out["atr14_pct"] = round(atr / ltp * 100.0, 2)
        vr = _last("VOLUME_RATIO", period=20)
        if vr is not None:
            out["vol_ratio20"] = round(vr, 2)
        for period in (50, 200):
            sma = _last("SMA", period=period)
            if sma and sma > 0:
                out[f"pct_from_{period}dma"] = round((ltp / sma - 1.0) * 100.0, 2)
        if len(close) >= 252:
            hi = float(close.iloc[-252:].max())
            if hi > 0:
                out["pct_from_52w_high"] = round((ltp / hi - 1.0) * 100.0, 2)

        # Strength vs the index over horizons a swing trade can actually act on.
        if nifty_close is not None:
            for label, bars in (("1m", 21), ("3m", 63)):
                ex = _excess_return_pct(close, nifty_close, bars)
                if ex is not None:
                    out[f"rs_excess_{label}_pct"] = ex
    except Exception as exc:  # noqa: BLE001
        _log.debug("swing_features_failed symbol=%s err=%s", symbol, str(exc)[:120])
        out.setdefault("last_close", 0.0)

    # The 12-month RS the ranking engine already computed and used to be discarded.
    if rs:
        for src_key, dst in (
            ("rs_percentile", "rs_percentile"),
            ("rs_score", "rs_excess_12m"),
            ("rs_momentum", "rs_momentum"),
            ("rs_new_high", "rs_at_new_high"),
        ):
            val = rs.get(src_key)
            if val is not None:
                out[dst] = round(val, 3) if isinstance(val, float) else val
    return out


def build_market_context(
    regime: str,
    vix: float,
    rs_top20: set[str],
    rs_blacklist: set[str],
    sector_status: dict[str, str],
    last_close: dict[str, float],
    fundamentals: dict[str, dict[str, Any]] | None = None,
    features: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the day's technical (+ optional fundamentals) snapshot.

    `last_close` (symbol -> price) scopes the eligible candidate universe to
    RS leaders with known, fresh pricing — the LLM is never offered a symbol
    it could hallucinate a price for. `features` (symbol -> build_symbol_features)
    supplies the RS-vs-index and technical detail; when absent the universe degrades
    to the old bare {symbol, last_close} shape rather than failing.
    """
    leading_sectors = [s for s, status in sector_status.items() if status == "LEADING"]
    declining_sectors = [s for s, status in sector_status.items() if status == "DECLINING"]
    features = features or {}
    candidate_universe = [
        features.get(sym) or {"symbol": sym, "last_close": last_close[sym]}
        for sym in sorted(rs_top20)
        if sym in last_close and sym not in rs_blacklist
    ]
    ctx: dict[str, Any] = {
        "_features": features,  # side-channel for ranking/logging, stripped before prompting
        "regime": regime,
        "vix": vix,
        "leading_sectors": leading_sectors,
        "declining_sectors": declining_sectors,
        "candidate_universe": candidate_universe,
    }
    # Only the candidates: the model can only propose symbols in candidate_universe, so
    # fundamentals for the other ~480 cached symbols are pure token waste. Passing the whole
    # cache made ONE origination call ~42k tokens (150k of 163k prompt chars were fundamentals
    # nobody could act on) -- 2026-09-24.
    if fundamentals:
        _cand_syms = {c.get("symbol") for c in candidate_universe}
        _scoped = {s: f for s, f in fundamentals.items() if s in _cand_syms}
        if _scoped:
            ctx["fundamentals"] = _scoped
    return ctx


def exclude_recent_exits(
    candidates: set[str], recently_exited: set[str]
) -> tuple[set[str], list[str]]:
    """Drop stocks we exited within the cooldown window from the candidate list BEFORE the LLM sees
    it. Returns (kept, dropped-sorted). Matches with or without the 'NSE:' prefix."""
    gone = {s.replace("NSE:", "") for s in recently_exited}
    dropped = sorted(c for c in candidates if c.replace("NSE:", "") in gone)
    return {c for c in candidates if c not in dropped}, dropped


def _build_memory_summary(recent_runs: list[Any], limit: int) -> str:
    """Compact text summary of recent journal rows for the prompt — trade_id
    outcome lookups (win/loss) are a future enhancement; for now this reflects
    what the agent decided and how many candidates it proposed each day."""
    if not recent_runs:
        return "(no prior runs yet)"
    lines = []
    for row in recent_runs[:limit]:
        n_candidates = len(json.loads(row["candidates_json"]) if row["candidates_json"] else [])
        lines.append(
            f"  {row['run_date']}: decision={row['decision']} "
            f"candidates={n_candidates} thesis={(row['thesis_text'] or '')[:120]!r}"
        )
    return "\n".join(lines)


def build_prompt(
    market_context: dict[str, Any], recent_runs: list[Any], memory_lookback: int
) -> str:
    # Underscore-prefixed keys are internal side-channels (e.g. _features, carried for
    # ranking/logging). Serialising them would duplicate the entire universe inside the
    # prompt and waste a large share of the token budget on data the model already has.
    _shown = {k: v for k, v in market_context.items() if not str(k).startswith("_")}
    lines = [
        "MARKET_CONTEXT:",
        # Compact, not indent=2 (2026-09-23): candidate_universe carries ~20 symbols x
        # 11 fields, and pretty-printing it costs ~390 tokens/call (measured, ~13% of
        # the whole swing prompt) in pure indentation whitespace the model does not
        # need to parse JSON. Zero information lost -- the DEBUG log (llm_asked in
        # src/llm/providers.py) still shows the raw sent text either way, readable or
        # not, since that is captured separately from what actually goes to the model.
        json.dumps(_shown, separators=(",", ":")),
        "",
        "YOUR_RECENT_DECISIONS (memory):",
        _build_memory_summary(recent_runs, memory_lookback),
        "",
        "Respond with JSON only.",
    ]
    return "\n".join(lines)


def _validate_candidate(raw: dict[str, Any], last_close: dict[str, float]) -> SwingCandidate | None:
    try:
        symbol = str(raw["symbol"]).strip()
        entry = float(raw["entry_price"])
        sl = float(raw["stop_loss"])
        target = float(raw["target"])
        confidence = float(raw.get("confidence", 0.5))
        reasoning = str(raw.get("reasoning", ""))[:400]
    except (KeyError, TypeError, ValueError) as exc:
        _log.warning("swing_candidate_malformed err=%s raw=%r", exc, raw)
        return None

    anchor = last_close.get(symbol)
    if anchor is None:
        _log.warning("swing_candidate_rejected reason=unknown_symbol symbol=%s", symbol)
        return None
    if anchor <= 0 or abs(entry - anchor) / anchor > _MAX_ENTRY_DRIFT_PCT:
        _log.warning(
            "swing_candidate_rejected reason=price_drift symbol=%s entry=%s anchor=%s",
            symbol,
            entry,
            anchor,
        )
        return None
    if not (0.0 < sl < entry < target):
        _log.warning(
            "swing_candidate_rejected reason=invalid_levels symbol=%s entry=%s sl=%s target=%s",
            symbol,
            entry,
            sl,
            target,
        )
        return None
    confidence = max(0.0, min(1.0, confidence))
    return SwingCandidate(symbol, entry, sl, target, confidence, reasoning)


def mechanical_score(cand: SwingCandidate, feat: dict[str, Any] | None) -> float:
    """An explainable cross-check on the model's own ranking — NOT the decider.

    Deliberately crude and readable: reward/risk, strength vs the index, and a penalty
    for buying something already extended. It exists so a disagreement between the
    model's confidence and the arithmetic is visible in the log, rather than trusting
    a single unvalidated opinion about which idea is best."""
    risk = cand.entry_price - cand.stop_loss
    rr = ((cand.target - cand.entry_price) / risk) if risk > 0 else 0.0
    score = min(rr, 4.0) / 4.0 * 0.5  # R:R, saturating — a 10R "target" is not 2.5x better
    if feat:
        ex = feat.get("rs_excess_1m_pct")
        if isinstance(ex, int | float):
            score += max(-1.0, min(1.0, float(ex) / 10.0)) * 0.3  # beating the index
        rsi = feat.get("rsi14")
        if isinstance(rsi, int | float):
            # Prefer constructive-but-not-blown-off. >75 is chasing, <40 is a falling knife.
            score += 0.2 if 45.0 <= float(rsi) <= 70.0 else 0.0
    return round(score, 4)


def select_best_candidates(
    candidates: list[SwingCandidate],
    cap: int,
    features: dict[str, dict[str, Any]] | None = None,
) -> list[SwingCandidate]:
    """Rank the model's ideas by ITS OWN probability estimate, then take the top `cap`.

    Previously this was `candidates[:cap]` — emission order — so on a day the model
    produced more ideas than the cap, the best one could be discarded simply for being
    listed fourth, and nothing recorded that it had existed. The model's `confidence` is
    its probability judgement, so that is what orders them; mechanical_score is logged
    alongside purely so the two can be compared over time before either is trusted."""
    if not candidates:
        return []
    features = features or {}
    ranked = sorted(
        candidates,
        key=lambda c: (-float(c.confidence), c.symbol),  # symbol breaks ties deterministically
    )
    for pos, c in enumerate(ranked, 1):
        feat = features.get(c.symbol)
        risk = c.entry_price - c.stop_loss
        _log.debug(
            "swing_candidate_ranked pos=%d symbol=%s llm_confidence=%.2f mech_score=%.3f "
            "rr=%.2f rs_excess_1m=%s rsi14=%s verdict=%s",
            pos,
            c.symbol,
            c.confidence,
            mechanical_score(c, feat),
            ((c.target - c.entry_price) / risk) if risk > 0 else 0.0,
            (feat or {}).get("rs_excess_1m_pct"),
            (feat or {}).get("rsi14"),
            "kept" if pos <= max(0, cap) else "dropped_below_cap",
        )
    if len(ranked) > max(0, cap):
        _log.info(
            "swing_candidates_ranked_and_capped proposed=%d cap=%d kept=%s dropped=%s",
            len(ranked),
            cap,
            [c.symbol for c in ranked[:cap]],
            [c.symbol for c in ranked[cap:]],
        )
    return ranked[: max(0, cap)]


def parse_llm_response(
    raw_text: str, last_close: dict[str, float]
) -> tuple[str | None, list[SwingCandidate]]:
    """Parse + validate the LLM's JSON response. Never raises — a malformed
    response degrades to (None, []), i.e. no trade today, same fail-safe
    philosophy as the G9 gate."""
    # Unwrap a markdown fence / surrounding prose BEFORE parsing. Without this a
    # perfectly good idea wrapped in ```json was recorded as "no_trade_today" -- see
    # src/llm/response_parse.py for the LAURUSLABS case this cost.
    cleaned = extract_json_object(raw_text)
    try:
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON is not an object")
    except (json.JSONDecodeError, ValueError) as exc:
        _log.warning("swing_response_malformed err=%s raw=%r", exc, raw_text[:200])
        return None, []

    thesis = data.get("thesis")
    thesis = str(thesis)[:600] if thesis else None
    raw_candidates = data.get("candidates", [])
    if not isinstance(raw_candidates, list):
        return thesis, []

    candidates: list[SwingCandidate] = []
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        cand = _validate_candidate(raw, last_close)
        if cand is not None:
            candidates.append(cand)
    return thesis, candidates


async def run_daily_reasoning(
    llm_client: Any,
    market_context: dict[str, Any],
    recent_runs: list[Any],
    last_close: dict[str, float],
    memory_lookback_days: int,
    max_candidates_per_day: int,
    timeout_secs: float = 30.0,
    limits: SwingLimits | None = None,
) -> SwingRunResult:
    """Run one daily reasoning pass. Returns a result with an empty candidate
    list (decision='no_trade_today') on any failure — never raises, mirroring
    trade_evaluator.evaluate_trade's fail-open-to-neutral contract, except here
    'fail open' means 'propose nothing' since there is no DSL fallback signal."""
    import asyncio

    limits = limits or SwingLimits(max_candidates=max_candidates_per_day)
    _universe = market_context.get("candidate_universe", [])
    _log.debug(
        "run_daily_reasoning_received candidate_universe=%d recent_runs=%d "
        "max_candidates_per_day=%d timeout_secs=%.1f regime=%s vix=%s leading_sectors=%s "
        "min_rr=%.2f max_sl_pct=%.1f spendable_cash=%.0f",
        len(_universe),
        len(recent_runs),
        max_candidates_per_day,
        timeout_secs,
        market_context.get("regime"),
        market_context.get("vix"),
        market_context.get("leading_sectors"),
        limits.min_reward_risk,
        limits.max_stop_loss_pct,
        limits.spendable_cash,
    )
    # The universe as the model actually sees it. Without this a pass that returns
    # nothing is unexplainable after the fact -- you cannot tell a genuinely poor
    # opportunity set from a starved or broken one.
    for _f in _universe:
        _log.debug(
            "run_daily_reasoning_offered %s",
            " ".join(f"{k}={v}" for k, v in _f.items()) if isinstance(_f, dict) else _f,
        )
    prompt = build_prompt(market_context, recent_runs, memory_lookback_days)
    model_used = getattr(llm_client, "_provider", None)
    model_used = str(model_used) if model_used is not None else None

    try:
        raw_text = await asyncio.wait_for(
            llm_client.complete(
                prompt, system=build_system_prompt(limits), temperature=0.4, max_tokens=3000
            ),
            timeout=timeout_secs,
        )
    except Exception as exc:
        _log.warning("swing_llm_call_failed err=%s", exc)
        return SwingRunResult(
            market_context_summary=market_context,
            thesis_text=None,
            llm_model_used=model_used,
        )
    # The call may have been answered by a FALLBACK provider (e.g. gemini down ->
    # groq), not the configured primary set above — last_provider (populated by
    # LLMClient.complete on success) is who actually originated this idea, and is
    # what the journal / logs must report so an audit isn't misattributed to a
    # provider that never actually answered.
    used = getattr(llm_client, "last_provider", None)
    if used is not None:
        model_used = str(used)

    thesis, candidates = parse_llm_response(raw_text, last_close)
    candidates = select_best_candidates(
        candidates, max_candidates_per_day, features=market_context.get("_features")
    )
    # "proposed", not "traded" — whether any candidate actually reaches a broker
    # is decided downstream by the CRM gate sequence; the caller (orchestrator's
    # _llm_swing_scan_loop) overwrites this with the real outcome
    # ("traded" / "all_blocked" / "no_trade_today") before persisting the journal row.
    decision = "proposed" if candidates else "no_trade_today"
    _log.debug(
        "run_daily_reasoning_found model=%s decision=%s candidates=%d symbols=%s",
        model_used,
        decision,
        len(candidates),
        [c.symbol for c in candidates],
    )
    return SwingRunResult(
        market_context_summary=market_context,
        thesis_text=thesis,
        candidates=candidates,
        decision=decision,
        llm_model_used=model_used,
        raw_llm_response=raw_text,
    )
