"""The seller-desk loop must not place or close anything while NSE is shut.

2026-10-16 00:05: with the market closed (quotes frozen), the date roll to DTE 3 made the desk
close a condor at stale prices (-Rs2,059), and two bull put spreads were then opened at 00:10 and
00:15 against the same frozen chain.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from src.orchestrator import _options_desk_market_open

_IST = ZoneInfo("Asia/Kolkata")


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 10, 16, 0, 5, tzinfo=_IST), False),  # Fri midnight -- the incident
        (datetime(2026, 10, 16, 9, 14, tzinfo=_IST), False),  # pre-open
        (datetime(2026, 10, 16, 9, 15, tzinfo=_IST), True),
        (datetime(2026, 10, 16, 14, 35, tzinfo=_IST), True),
        (datetime(2026, 10, 16, 15, 30, tzinfo=_IST), True),
        (datetime(2026, 10, 16, 15, 31, tzinfo=_IST), False),
        (datetime(2026, 10, 17, 11, 0, tzinfo=_IST), False),  # Saturday
    ],
)
def test_options_desk_market_open(when: datetime, expected: bool) -> None:
    assert _options_desk_market_open(when) is expected
