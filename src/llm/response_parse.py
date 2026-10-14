"""Extract a JSON object from an LLM reply — one implementation, used by every path.

Models are told "respond with JSON only" and mostly comply, but every provider
occasionally wraps the object in a markdown code fence or prepends a sentence, because
that is how they are trained to present code to a human. That formatting is not a
refusal and not a malformed answer -- the trade is in there.

This mattered in real money: on 2026-09-16 gemini returned a valid LAURUSLABS swing idea
(entry 1901.70 / sl 1825 / target 2055, R:R 2.0 -- comfortably through the 1.8 gate)
wrapped in ```json. llm_swing called json.loads() on the raw string, threw, and recorded
the run as "no_trade_today". The same name was eventually bought five days later at
1973.80 -- ~3.7% worse. Worse than the slippage, the journal then showed a parsing
failure as though it were a deliberate decision to stand aside, which silently
contaminates every "was the agent too conservative?" measurement taken afterwards.

Two-stage on purpose: strip a fence if there is one, then fall back to slicing between
the outermost braces, which also rescues a reply with prose wrapped around the object.
"""

from __future__ import annotations


def strip_code_fence(text: str) -> str:
    """Remove a leading ```/```json fence and its closing ``` if present."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    t = t.split("\n", 1)[1] if "\n" in t else ""
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    return t.strip()


def extract_json_object(text: str) -> str:
    """Best-effort JSON object from an LLM reply. Never raises — returns the original
    text when nothing better can be found, so the caller's own json.loads() produces
    the error (and the log line) rather than this helper inventing one."""
    if not text:
        return text
    unfenced = strip_code_fence(text)
    if unfenced.startswith("{"):
        return unfenced
    # Prose around the object (e.g. "Here is my analysis: {...}") — take the outermost
    # braces. rfind, not find, so a trailing explanation after the object is dropped too.
    start, end = unfenced.find("{"), unfenced.rfind("}")
    if start != -1 and end != -1 and end > start:
        return unfenced[start : end + 1]
    return unfenced
