"""Stacks are keyed "paper" / "live:<broker>" (multi-broker phase 2, task 4).

Covers the orchestrator's stack picker, the boot-time "which live brokers do we need"
decision, and a grep guard against leftover two-key ("paper"/"live") assumptions.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import src.orchestrator as orch
from src.brokers.routing import BrokerRoute

_ROOT = Path(__file__).parents[3]

PAPER = {"lifecycle": "paper_lc"}
KITE = {"lifecycle": "kite_lc"}
ANGEL = {"lifecycle": "angel_lc"}
LEGACY = {"lifecycle": "legacy_lc"}


# ── _pick_stack ──────────────────────────────────────────────────────────────


def test_pick_paper_stack() -> None:
    stacks = {"paper": PAPER, "live:kite": KITE}
    assert orch._pick_stack(stacks, True) is PAPER


def test_pick_live_by_broker() -> None:
    stacks = {"paper": PAPER, "live:kite": KITE, "live:angel_one": ANGEL}
    assert orch._pick_stack(stacks, False, "angel_one") is ANGEL
    assert orch._pick_stack(stacks, False, "kite") is KITE


def test_pick_live_without_broker_uses_primary_live_stack() -> None:
    assert orch._pick_stack({"paper": PAPER, "live:angel_one": ANGEL}, False) is ANGEL
    # live:kite is the primary when present (legacy broker)
    stacks = {"paper": PAPER, "live:angel_one": ANGEL, "live:kite": KITE}
    assert orch._pick_stack(stacks, False) is KITE


def test_pick_without_broker_prefers_the_legacy_live_broker() -> None:
    # global.json broker.name=angel_one; a forced live:kite stack must not become primary
    stacks = {"paper": PAPER, "live:angel_one": ANGEL, "live:kite": KITE}
    shared = {"legacy_live_broker": "angel_one", "stacks": stacks}
    assert orch._pick_stack(shared, False) is ANGEL
    assert orch._pick_stack({**shared, "legacy_live_broker": "kite"}, False) is KITE
    # no legacy stack -> previous behaviour (live:kite preferred)
    assert orch._pick_stack({**shared, "legacy_live_broker": "flattrade"}, False) is KITE


def test_pick_tolerates_legacy_bare_live_key() -> None:
    assert orch._pick_stack({"paper": PAPER, "live": LEGACY}, False) is LEGACY


def test_pick_missing_returns_none() -> None:
    assert orch._pick_stack({"paper": PAPER}, False) is None
    assert orch._pick_stack({"live:kite": KITE}, True) is None
    assert orch._pick_stack({"paper": PAPER, "live:kite": KITE}, False, "angel_one") is None
    assert orch._pick_stack({}, False) is None


def test_pick_accepts_shared_dict() -> None:
    shared = {"paper_mode": True, "stacks": {"paper": PAPER, "live:kite": KITE}}
    assert orch._pick_stack(shared, False) is KITE
    assert orch._pick_stack(shared, True) is PAPER
    # a shared dict without stacks (callers/tests not stack-aware) -> None
    assert orch._pick_stack({"paper_mode": True, "crm": object()}, True) is None


def test_every_live_stack_reseeds_with_its_own_broker_filter() -> None:
    # Task 5 replaced the "only paper + primary reseed" guard: every live stack reseeds,
    # filtered to its own exec_broker; the KITE stack (not the primary) also owns unstamped
    # (NULL), unknown and pre-089 rows, because NULL means kite.
    stacks = {"paper": PAPER, "live:kite": KITE, "live:angel_one": ANGEL}
    assert not hasattr(orch, "_reseeds_own_rows")
    assert orch._reseed_scope("paper", stacks) == {}
    assert orch._reseed_scope("live:angel_one", stacks)["broker_name"] == "angel_one"
    assert "unfiltered_if_no_column" not in orch._reseed_scope("live:angel_one", stacks)
    assert orch._reseed_scope("live:kite", stacks)["unfiltered_if_no_column"] is True
    # whichever broker is the legacy (primary) one
    assert orch._reseed_scope("live:kite", stacks, "angel_one")["unfiltered_if_no_column"]
    assert "unfiltered_if_no_column" not in orch._reseed_scope(
        "live:angel_one", stacks, "angel_one"
    )


# ── intraday smart-exit finds the owning monitor across stacks ───────────────


class _Mon:
    def __init__(self, tids: set[str]) -> None:
        self._tids = tids

    def get(self, tid: str) -> object | None:
        return object() if tid in self._tids else None


def test_monitor_tracking_searches_every_stack() -> None:
    first, second = _Mon(set()), _Mon({"T1"})
    shared = {
        "order_monitor": first,  # the primary alias does NOT track it
        "stacks": {"paper": {"order_monitor": first}, "live:kite": {"order_monitor": second}},
    }
    assert orch._monitor_tracking(shared, "T1") is second
    assert orch._monitor_tracking(shared, "T-GONE") is None


def test_monitor_tracking_falls_back_to_the_primary_alias_without_stacks() -> None:
    mon = _Mon({"T1"})
    assert orch._monitor_tracking({"order_monitor": mon}, "T1") is mon
    assert orch._monitor_tracking({"order_monitor": mon}, "T2") is None
    assert orch._monitor_tracking({}, "T1") is None


def test_intraday_smart_exit_uses_the_owning_monitor() -> None:
    src = (_ROOT / "src" / "orchestrator.py").read_text(encoding="utf-8")
    block = src[src.index('_ex = shared.get("intraday_exit_state", {}).get(sym)') :][:400]
    assert '_om = _monitor_tracking(shared, _ex["tid"])' in block
    assert 'shared.get("order_monitor")' not in block


def test_mode_label() -> None:
    assert orch.mode_label("paper") == "paper"
    assert orch.mode_label("live") == "live"
    assert orch.mode_label("live:kite") == "live"
    assert orch.mode_label("live:angel_one") == "live"


# ── needed_live_brokers ──────────────────────────────────────────────────────

_ALL_PAPER = {"intraday": True, "swing": True, "options": True}
_SWING_LIVE = {"intraday": True, "swing": False, "options": True}


def test_locked_needs_no_live_broker() -> None:
    routes = {"s": BrokerRoute("angel_one", None)}
    assert orch.needed_live_brokers(routes, _SWING_LIVE, "kite", True, True) == []


def test_one_live_strategy_on_angel_one() -> None:
    routes = {"s": BrokerRoute("angel_one", None)}
    assert orch.needed_live_brokers(routes, _ALL_PAPER, "kite", False, False) == ["angel_one"]


def test_live_segment_plus_routed_strategy_legacy_first_deduped() -> None:
    routes = {
        "a": BrokerRoute("angel_one", None),
        "b": BrokerRoute("kite", None),
        "c": BrokerRoute("angel_one", None),
    }
    assert orch.needed_live_brokers(routes, _SWING_LIVE, "kite", False, False) == [
        "kite",
        "angel_one",
    ]


def test_legacy_needed_flag_alone_builds_legacy() -> None:
    assert orch.needed_live_brokers({}, _ALL_PAPER, "kite", False, True) == ["kite"]


def test_nothing_live_needs_nothing() -> None:
    assert orch.needed_live_brokers({}, _ALL_PAPER, "kite", False, False) == []


def test_unusable_routes_build_nothing() -> None:
    routes = {"s": BrokerRoute(None, "live strategy has no 'broker' field")}
    assert orch.needed_live_brokers(routes, _ALL_PAPER, "kite", False, False) == []


# ── grep guard ───────────────────────────────────────────────────────────────

# Literal patterns that assume exactly two stacks keyed "paper" / "live".
_PATTERNS = [
    re.compile(r'\.get\(\s*"live"'),
    re.compile(r'\[\s*"live"\s*\]'),
    re.compile(r'"live"\s+(not\s+)?in\b'),
    re.compile(r'[!=]=\s*"paper"'),
    re.compile(r'\.get\(\s*"paper"\s+if\b'),
    re.compile(r'[!=]=\s*"live"'),
    re.compile(r"\b(stack_key|_stack_mode|_rc_mode|_mode_name)\s*[!=]="),
    re.compile(r'stacks\[\s*"paper"\s+if\b'),
    re.compile(r'else\s+"live"\s*\]'),
]

# Every line that still matches is a legitimate NON-stack use. Exact stripped line
# content -> number of occurrences allowed. A new stack-key literal (or a new copy
# of one of these lines) fails the test and must be checked by a human.
_ALLOWED: dict[str, dict[str, int]] = {
    "src/orchestrator.py": {
        # _resolve_is_paper: registry execution_mode values, not stack keys
        'if _override == "paper":': 1,
        # _refresh_daily_counters: CRM mode labels ("paper"/"live") over the
        # paper_trades/trades tables, not stack keys
        '["live"] if any(not stack_is_paper(k) for k in _stacks) else []': 1,
        '_table = "paper_trades" if _mode == "paper" else "trades"': 1,
        'if _mode == "paper":': 1,
        # learning loops: per-mode outcome buckets fed from paper_trades / trades
        '_regime_trades["live"].setdefault((strat, regime), []).append(': 1,
        '_universe_outcomes["live"].setdefault(strat, []).append(t)': 1,
        'if mode_key == "paper" and not _new_trades_paper:': 2,
        # _fetch_closed_trades_from_db(mode=...): table selector argument
        'elif mode == "paper":': 1,
        # options exit tracking: segment mode label from _segment_mode()
        '_opt_is_paper = _segment_mode(shared, "options") == "paper"': 1,
        # learning promotion: registry execution_mode value
        'shared.get("strategy_execution_mode", {}).get(name) == "paper"': 1,
        # --- `== "live"` / `!= "live"` (all verified non-stack) ---
        # _resolve_is_paper: registry execution_mode value
        'if _override == "live":': 1,
        # _enforce_intraday_daily_loss_breaker(mode=CRM mode label) and
        # _fetch_closed_trades_from_db(mode=table selector)
        'if mode == "live":': 2,
        # _enforce_intraday_daily_loss_breaker: CRM mode label guard before the paper flatten
        'if mode != "paper":': 1,
        # learning loops: per-mode outcome buckets (paper_trades vs trades)
        'if mode_key == "live" and not _new_trades:': 2,
        '_log_path = _cal_log if _fetch_mode == "live" else _cal_log_paper': 1,
        '_model_tag = _strat if _fetch_mode == "live" else f"{_strat}::paper"': 1,
        'if _fetch_mode != "live":': 1,
        '_log_path = _learn_log if _fetch_mode == "live" else _learn_log_paper': 1,
        # config/learning.json mode switches ("off"/"shadow"/"live"), incl. docstrings
        '_learning_mode("strategy_selector") == "live"': 1,
        'if _signal_ranker_mode("intraday") == "live":': 2,
        '_blocked_by_gate = _needs_gate and _resume_mode == "live" and not _gate_passed': 1,
        'if _learning_mode("strategy_selector") == "live" and _pw is not None:': 1,
        'mode=="live" (so rank_candidates_by_sqs applies it); in "shadow" gating': 1,
        'use_sqs=(_signal_ranker_mode("swing") == "live"),': 1,
        'Requires config/learning.json options_structure_exit_tracking.mode=="live"': 1,
        'mode=="live"; this loop\'s own persistence is identical either way so the': 1,
        '_dry_run = _learning_mode("promotion") != "live"': 1,
        'if _learning_mode("failure_scorer") == "live":': 1,
        'if _learning_mode("causal_memory") == "live":': 1,
        # boot: options desk registry execution_mode override
        '_options_is_paper = _opt_override == "paper"': 1,
    },
    "src/notifications/summary.py": {},
}


def _offending(rel: str) -> Counter[str]:
    hits: Counter[str] = Counter()
    for line in (_ROOT / rel).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        if any(p.search(s) for p in _PATTERNS):
            hits[s] += 1
    return hits


def test_no_two_key_stack_literals_left() -> None:
    problems: list[str] = []
    for rel, allowed in _ALLOWED.items():
        for line, n in _offending(rel).items():
            if n > allowed.get(line, 0):
                problems.append(f"{rel}: {line!r} x{n} (allowed {allowed.get(line, 0)})")
    assert not problems, "two-key stack literal(s):\n" + "\n".join(problems)


def test_allow_list_has_no_stale_entries() -> None:
    stale: list[str] = []
    for rel, allowed in _ALLOWED.items():
        found = _offending(rel)
        stale += [f"{rel}: {line!r}" for line in allowed if found.get(line, 0) == 0]
    assert not stale, "allow-list entries no longer present:\n" + "\n".join(stale)


def test_guard_patterns_catch_the_two_key_forms() -> None:
    for bad in (
        'x = stacks.get("live")',
        'x = stacks["live"]',
        'if "live" in stacks:',
        'is_paper = stack_key == "paper"',
        'if stack_key == "live":',
        "p = _stack_mode == something",
        "p = _rc_mode != other",
        "p = _mode_name == other",
        's = _stacks.get("paper" if p else "live")',
        's = stacks["paper" if p else "live"]',
        's = stacks[k if p else "live"]',
    ):
        assert any(p.search(bad) for p in _PATTERNS), bad
