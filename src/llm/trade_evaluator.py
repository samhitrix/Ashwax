"""
G9 LLM trade confirmation — advisory evaluator.

Per architecture Gate 9: the LLM scores a candidate signal that has ALREADY
passed the hard risk gates and returns reasoning. ADVISORY ONLY — CRM downgrades
any G9 BLOCK to a flag; the LLM can never override gates 0-8. Output populates
signal.meta:
    llm_confidence : float 0.0-1.0   (also read by G3 pre-close window)
    llm_flag       : "CONFIRM" | "REJECT" | "NEUTRAL"
    llm_reason     : str

Every call (prompt context + verdict + latency) is logged to llm.log. Malformed
responses are validated via sanity_watchdog; on any failure the gate falls back
to neutral 0.50 so trading continues DSL-only (never raises).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from src.infrastructure.logger import get_logger
from src.llm.sanity_watchdog import validate_llm_response

_log = get_logger("llm")

_SYSTEM = (
    "You are a component in an automated quantitative trading system, called "
    "programmatically as one gate in a larger risk pipeline — not a chatbot "
    "talking to a person, and nothing you output is shown to or acted on "
    "directly by any human. No advice is being solicited or given. "
    "You receive a candidate trade that has ALREADY passed hard, code-enforced "
    "risk gates (position sizing, stop-loss, exposure caps) upstream of you. "
    "Your ONLY job is a final numeric sanity check: does this specific setup "
    "look internally coherent given its own strategy, strength, and historical "
    "stats? "
    "The `regime` field describes the broad index, not this individual stock — "
    "plenty of names move counter to the broad regime on their own relative "
    "strength, sector rotation, or catalyst, and that is a normal, tradeable "
    "situation, not a defect. Position sizing already accounts for the added "
    "uncertainty of a counter-regime trade upstream of you, so do not REJECT "
    "or lower confidence for regime misalignment alone — judge the setup on "
    "its own merits (does the stop/target/entry make sense, do the stats and "
    "reasoning hold together) and only flag REJECT for a genuine flaw there. "
    "If regime misalignment is a real added risk worth noting, say so in your "
    "one-sentence reasoning so it's logged, but let a coherent individual "
    "setup pass. "
    "You are advisory only — your output cannot override any risk gate, only "
    "flag a note for logging. "
    "Respond ONLY with compact JSON, no other text: "
    '{"confidence": <float 0..1>, "flag": "CONFIRM|REJECT|NEUTRAL", '
    '"reasoning": "<one short sentence>"}.'
)


def _build_prompt(sig: dict[str, Any]) -> str:
    lines = [
        "CANDIDATE TRADE:",
        f"  symbol: {sig.get('symbol')}",
        f"  direction: {sig.get('direction')}",
        f"  strategy: {sig.get('strategy_name')}",
        f"  regime: {sig.get('regime')}",
    ]
    # entry_price/stop_loss_pct/target_pct are meaningless for an options
    # credit structure (seller_crm_adapter.py sets them to synthetic
    # CRM-sizing placeholders, not real economics) — omit rather than show
    # a misleading number; max_loss/max_profit/lots below carry the real
    # economics for that case.
    if sig.get("entry_price") is not None:
        lines.append(f"  entry: {sig.get('entry_price')}")
    if sig.get("stop_loss_pct") is not None:
        lines.append(f"  stop_loss_pct: {sig.get('stop_loss_pct')}")
    if sig.get("target_pct") is not None:
        lines.append(f"  target_pct: {sig.get('target_pct')}")
    if sig.get("max_loss") is not None:
        lines.append(f"  max_loss: {sig.get('max_loss')}")
    if sig.get("max_profit") is not None:
        lines.append(f"  max_profit: {sig.get('max_profit')}")
    if sig.get("lots") is not None:
        lines.append(f"  lots: {sig.get('lots')}")
    lines += [
        f"  strategy_health: {sig.get('strategy_health')}",
        f"  vix: {sig.get('vix')}",
        f"  rs_new_high: {sig.get('rs_new_high')}",
    ]
    causal = sig.get("causal_context")
    if causal:
        lines.append(f"  past_lessons: {causal}")
    news_ctx = sig.get("news_context")
    if news_ctx:
        lines.append(f"  recent_news: {news_ctx}")
    lines.append("")
    lines.append("Respond with JSON only.")
    return "\n".join(lines)


def _load_gate_timeout() -> float:
    """llm.trade_gate_timeout_seconds from global.json — hard cap on the G9 call.

    A provider that is down (or pointed at a model it does not have) used to block the
    entry for its full connect/read timeout, which on 2026-08-25 was 11-14s per signal
    and cost every intraday entry its fill. G9 fails OPEN anyway, so waiting longer
    buys nothing.
    """
    try:
        import json as _j
        from pathlib import Path as _P

        _cfg = _j.loads((_P("config") / "global.json").read_text())
        return float(_cfg.get("llm", {}).get("trade_gate_timeout_seconds", 3.0))
    except Exception:
        return 3.0


_GATE_TIMEOUT_SECS = _load_gate_timeout()


async def evaluate_trade_gated(
    client: Any,
    sig: dict[str, Any],
    consecutive_malformed: int = 0,
    chain_timeout_secs: float | None = None,
) -> dict[str, Any]:
    """G9 check for a strategy whose `llm_gate_required` is true.

    Differs from a bare evaluate_trade() in the two ways that decided the
    2026-09-21 review: (1) `chain_timeout_secs` is the budget for the WHOLE
    provider chain (groq -> cloudflare -> ollama ...), sized so a slow or rate-
    limited first provider cannot cancel the chain before the next one answers
    (the shared 3s cap did exactly that); (2) a missing verdict is re-checked
    once with a fresh chain before it is reported as "no verdict". The result
    adds `provider` (which one answered) and `attempts`.
    """
    _log.debug(
        "g9_gated_received symbol=%s strategy=%s chain_timeout_secs=%s",
        sig.get("symbol"),
        sig.get("strategy_name"),
        chain_timeout_secs,
    )
    verdict = await evaluate_trade(
        client, sig, consecutive_malformed, timeout_secs=chain_timeout_secs
    )
    attempts = 1
    if not verdict["evaluated"]:
        _log.debug(
            "g9_gated_retry symbol=%s attempt1_reason=%s",
            sig.get("symbol"),
            verdict.get("llm_reason"),
        )
        verdict = await evaluate_trade(
            client, sig, int(verdict["malformed"]), timeout_secs=chain_timeout_secs
        )
        attempts = 2
    prov = getattr(client, "last_provider", None)
    verdict["provider"] = getattr(prov, "value", None) if verdict["evaluated"] else None
    verdict["attempts"] = attempts
    if not verdict["evaluated"]:
        verdict["llm_reason"] = f"no verdict after {attempts} attempts: {verdict['llm_reason']}"
    return verdict


async def evaluate_trade(
    client: Any,
    sig: dict[str, Any],
    consecutive_malformed: int = 0,
    timeout_secs: float | None = None,
) -> dict[str, Any]:
    """
    Run the G9 advisory LLM check.

    Returns dict: llm_confidence, llm_flag, llm_reason, malformed, latency_ms,
    evaluated. `evaluated` is True only when the model returned a real, valid
    verdict; on error / malformed / no-provider it is False so the blocking G9
    gate can fail OPEN (an LLM outage must never halt trading).
    Never raises — on any failure returns neutral 0.50 / NEUTRAL.
    """
    # Full audit record of what was actually submitted for review -- every field
    # _build_prompt() puts in front of the LLM, not just a summary, so a later
    # "why did G9 decide X" question can be answered from this line alone
    # without needing to reconstruct the candidate from other logs.
    _log.info(
        "g9_review_received symbol=%s strategy=%s direction=%s entry=%s stop_loss_pct=%s "
        "target_pct=%s max_loss=%s max_profit=%s lots=%s regime=%s strategy_health=%s vix=%s "
        "rs_new_high=%s has_causal_context=%s has_news_context=%s",
        sig.get("symbol"),
        sig.get("strategy_name"),
        sig.get("direction"),
        sig.get("entry_price"),
        sig.get("stop_loss_pct"),
        sig.get("target_pct"),
        sig.get("max_loss"),
        sig.get("max_profit"),
        sig.get("lots"),
        sig.get("regime"),
        sig.get("strategy_health"),
        sig.get("vix"),
        sig.get("rs_new_high"),
        bool(sig.get("causal_context")),
        bool(sig.get("news_context")),
    )
    prompt = _build_prompt(sig)
    t0 = time.perf_counter()
    try:
        raw = await asyncio.wait_for(
            client.complete(prompt, system=_SYSTEM),
            timeout=_GATE_TIMEOUT_SECS if timeout_secs is None else timeout_secs,
        )
    except TimeoutError as exc:
        _log.warning(
            "g9_llm_timeout",
            extra={"ctx": {"symbol": sig.get("symbol"), "err": str(exc)[:120]}},
        )
        return {
            "llm_confidence": 0.50,
            "llm_flag": "NEUTRAL",
            "llm_reason": "llm_timeout",
            "malformed": consecutive_malformed,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "evaluated": False,
        }
    except Exception as exc:
        _log.warning(
            "g9_llm_error",
            extra={"ctx": {"symbol": sig.get("symbol"), "err": str(exc)[:120]}},
        )
        return {
            "llm_confidence": 0.50,
            "llm_flag": "NEUTRAL",
            "llm_reason": f"llm_error:{exc}",
            "malformed": consecutive_malformed,
            "latency_ms": 0,
            "evaluated": False,
        }

    latency_ms = int((time.perf_counter() - t0) * 1000)
    raw = (raw or "").strip()
    # Extract the JSON object even if the model wrapped it in prose / code fences
    start = raw.find("{")
    end = raw.rfind("}")
    candidate = raw[start : end + 1] if start != -1 and end != -1 else raw

    vr = validate_llm_response(candidate, consecutive_malformed=consecutive_malformed)
    if not vr.valid:
        _log.warning(
            "g9_llm_malformed",
            extra={
                "ctx": {
                    "symbol": sig.get("symbol"),
                    "reason": vr.reason,
                    "consecutive": vr.malformed_count,
                    "latency_ms": latency_ms,
                    "raw": raw[:200],
                }
            },
        )
        return {
            "llm_confidence": 0.50,
            "llm_flag": "NEUTRAL",
            "llm_reason": vr.reason,
            "malformed": vr.malformed_count,
            "latency_ms": latency_ms,
            "evaluated": False,
        }

    data = json.loads(candidate)
    confidence = float(data["confidence"])
    flag = str(data.get("flag", "NEUTRAL")).upper()
    if flag not in ("CONFIRM", "REJECT", "NEUTRAL"):
        flag = "NEUTRAL"
    reasoning = str(data.get("reasoning", ""))

    # RS line making new highs → +0.10 confidence boost (capped at 1.0)
    if sig.get("rs_new_high"):
        _log.debug("g9_confidence_boost symbol=%s reason=rs_new_high", sig.get("symbol"))
        confidence = min(1.0, confidence + 0.10)

    # "Stocks to watch" priority aligned with signal direction → +0.10 boost
    if sig.get("news_priority_aligned"):
        _log.debug("g9_confidence_boost symbol=%s reason=news_priority_aligned", sig.get("symbol"))
        confidence = min(1.0, confidence + 0.10)

    # The decision + its full basis, in one line -- paired with g9_review_received
    # above (same symbol/strategy), this is the complete "what it reviewed ->
    # what it decided -> why" audit trail for one G9 review. Reasoning is
    # deliberately NOT truncated: the prompt asks for one short sentence, so
    # clipping it has only ever hidden the informative part on the rare longer
    # reply, never saved meaningful log volume.
    _log.info(
        "g9_llm_verdict",
        extra={
            "ctx": {
                "symbol": sig.get("symbol"),
                "strategy": sig.get("strategy_name"),
                "provider": getattr(getattr(client, "last_provider", None), "value", None),
                "confidence": round(confidence, 3),
                "flag": flag,
                "latency_ms": latency_ms,
                "basis": reasoning,
            }
        },
    )
    return {
        "llm_confidence": confidence,
        "llm_flag": flag,
        "llm_reason": reasoning,
        "malformed": 0,
        "latency_ms": latency_ms,
        "evaluated": True,
    }
