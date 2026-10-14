"""Absolute equity-floor percentage, read from config/capital.json.

``account.stop_all_trading_if_equity_falls_below`` (e.g. "25%" -> fraction 0.25).
Shared by CRM (src/risk/crm.py, src/risk/absolute_floor.py) and the Supervisor
(src/supervisor/floor_check.py). Only the *number* is shared: each of those keeps its
own independent check implementation so the two kill-switches still fail independently.

2026-10-07: moved from config/risk.json circuit_breakers.absolute_floor_pct (Phase 4 of
the capital.json redesign) -- that key duplicated this one and was never the thing
either kill-switch actually read, which risked silent drift between the two files.

The value is read once per process (restart to change it). An unreadable/unparseable
config or a value outside (0, 1) is rejected and the safe default is used with a
warning -- a typo must never silently disable the last-resort kill-switch.
"""

from __future__ import annotations

import logging
from functools import cache

DEFAULT_FLOOR_PCT = 0.25
_LOG = logging.getLogger(__name__)  # "src.risk.floor_config" -> risk.log


@cache
def absolute_floor_pct() -> float:
    try:
        from src.risk.budget import load_risk_budget

        value = load_risk_budget().account.stop_trading_below_equity_pct / 100.0
    except Exception:  # noqa: BLE001 -- config.* ValueError/KeyError/OSError, never crash the gate
        return DEFAULT_FLOOR_PCT
    if not 0.0 < value < 1.0:
        _LOG.warning(
            "absolute_floor_pct_invalid value=%s -- using default %.2f", value, DEFAULT_FLOOR_PCT
        )
        return DEFAULT_FLOOR_PCT
    return value
