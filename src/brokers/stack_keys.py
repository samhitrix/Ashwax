"""Keys for the orchestrator's execution stacks: "paper" or "live:<broker>".

The legacy bare key "live" is still accepted so older fixtures keep working; it means
"the single live stack" and carries no broker name.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LEGACY_LIVE_BROKER = "kite"
PAPER_KEY = "paper"
_LIVE_PREFIX = "live:"


def stack_key(is_paper: bool, broker: str | None = None) -> str:
    """Key of the stack that executes (paper) or the live stack of `broker`."""
    if is_paper:
        return PAPER_KEY
    return f"{_LIVE_PREFIX}{broker or LEGACY_LIVE_BROKER}"


def _check(key: str) -> None:
    if key not in (PAPER_KEY, "live") and not key.startswith(_LIVE_PREFIX):
        raise ValueError(f"unknown stack key {key!r}")


def stack_is_paper(key: str) -> bool:
    _check(key)
    return key == PAPER_KEY


def stack_broker(key: str) -> str | None:
    """Broker of a live key; None for paper and for the legacy bare "live" key."""
    _check(key)
    return key[len(_LIVE_PREFIX) :] if key.startswith(_LIVE_PREFIX) else None


def live_stacks(stacks: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in stacks.items() if not stack_is_paper(k)}


def primary_live_key(stacks: Mapping[str, Any], legacy_broker: str | None = None) -> str | None:
    """The live key legacy single-broker code should use: the legacy (global.json) broker's
    stack when given and present, else the only live key, else "live:kite", else the first."""
    keys = list(live_stacks(stacks))
    if not keys:
        return None
    if legacy_broker and stack_key(False, legacy_broker) in keys:
        return stack_key(False, legacy_broker)
    legacy = stack_key(False)
    return legacy if legacy in keys else keys[0]
