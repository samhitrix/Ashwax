"""
Real P&L Tracker — spec §8.15 canonical import path.

The arithmetic lives in src/risk/real_pnl.py.  This module re-exports it
so callers can use the spec-defined path `src.pnl.real_pnl_tracker`.
"""

from src.risk.real_pnl import (  # noqa: F401
    CostBreakdown,
    ProductType,
    RealPnL,
    calculate_real_pnl,
)
