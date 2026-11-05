"""Kite basket_order_margins reply -> OrderMarginResult. The first test uses the structure and totals
RECORDED from a live call on 2026-10-12 (two NIFTY weekly short legs); the old parser only knew a bare
"total" and rejected it, which blocked every options entry that morning."""

from __future__ import annotations

import pytest
from src.brokers.adapters.kite.adapter import _order_margin_from_kite

_LEG_KEYS = [
    "type",
    "tradingsymbol",
    "exchange",
    "span",
    "exposure",
    "option_premium",
    "additional",
    "bo",
    "cash",
    "var",
    "mtf",
    "pnl",
    "leverage",
    "charges",
    "total",
]


def _block(total: float) -> dict[str, object]:
    return {k: (total if k == "total" else 0) for k in _LEG_KEYS}


def test_real_basket_reply_uses_the_netted_final_total() -> None:
    reply = {
        "initial": _block(189981.22),
        "final": _block(184839.72),
        "orders": [_block(160708.535), _block(29272.685)],
        "charges": {"brokerage": 40.0, "total": 61.0},
    }
    res = _order_margin_from_kite(reply)
    assert res.total_margin == pytest.approx(
        184839.72
    )  # after hedge/netting, not the 'initial' figure
    assert res.per_leg == pytest.approx([160708.535, 29272.685])  # diagnostic only, never summed
    assert res.raw is reply


def test_data_wrapped_reply_is_unwrapped() -> None:
    res = _order_margin_from_kite(
        {"data": {"initial": _block(10.0), "final": _block(9.0), "orders": []}}
    )
    assert res.total_margin == 9.0 and res.per_leg == []


def test_legacy_shapes_still_work() -> None:
    assert _order_margin_from_kite({"total": 123.0}).total_margin == 123.0
    res = _order_margin_from_kite({"data": [{"total": 10.0}, {"total": 5.0}]})
    assert res.total_margin == 15.0 and res.per_leg == [10.0, 5.0]


@pytest.mark.parametrize(
    "bad", [{}, {"initial": {"total": 1.0}}, {"final": {"nope": 1}}, {"data": []}]
)
def test_an_unrecognised_shape_fails_loudly_never_guesses(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="unrecognized order-margin response shape"):
        _order_margin_from_kite(bad)
