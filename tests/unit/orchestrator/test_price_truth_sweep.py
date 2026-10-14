"""Regression test for the IFCI price-truth infinite-correction bug (2026-09-02):

_price_truth_phase2_sweep wrote a corrected fill_price to the DB but never
updated the in-memory OrderMonitor position, so the next sweep re-read the
same stale value and "corrected" it again, forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.execution.price_truth import PriceTruthResult, PriceTruthStatus
from src.orchestrator import _price_truth_phase2_sweep


@dataclass
class _FakePosition:
    agent_trade_id: str
    symbol: str
    fill_price: float
    quantity: int
    product: str = "CNC"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_sweep_updates_in_memory_fill_price_after_correction() -> None:
    pos = _FakePosition(agent_trade_id="TRD-1", symbol="IFCI", fill_price=84.27, quantity=145)

    price_truth = MagicMock()
    price_truth.phase2_event_gated = AsyncMock(
        return_value=PriceTruthResult(
            phase=2,
            status=PriceTruthStatus.CORRECTED,
            symbol="IFCI",
            broker_order_id="TRD-1",
            expected_price=84.27,
            actual_price=85.00,
            corrected_price=85.00,
        )
    )
    trade_repo = MagicMock()
    trade_repo.update_fill = AsyncMock()

    await _price_truth_phase2_sweep(price_truth, [pos], trade_repo)

    assert pos.fill_price == 85.00
    trade_repo.update_fill.assert_awaited_once()

    # Second sweep must compare against the now-corrected price, not the stale
    # 84.27 — the exact loop that fired every ~30s in production.
    await _price_truth_phase2_sweep(price_truth, [pos], trade_repo)
    _, kwargs = price_truth.phase2_event_gated.call_args
    assert kwargs["recorded_fill_price"] == 85.00


@pytest.mark.asyncio  # type: ignore[misc]
async def test_sweep_passes_product_through_to_phase2() -> None:
    pos = _FakePosition(
        agent_trade_id="TRD-1", symbol="IFCI", fill_price=84.27, quantity=145, product="CNC"
    )
    price_truth = MagicMock()
    price_truth.phase2_event_gated = AsyncMock(
        return_value=PriceTruthResult(
            phase=2, status=PriceTruthStatus.OK, symbol="IFCI", broker_order_id="TRD-1"
        )
    )
    trade_repo = MagicMock()

    await _price_truth_phase2_sweep(price_truth, [pos], trade_repo)

    _, kwargs = price_truth.phase2_event_gated.call_args
    assert kwargs["product"] == "CNC"
