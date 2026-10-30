"""The Options tab's NRML/MIS badge (2026-10-29)."""

from __future__ import annotations

import json

from src.api.server import _option_product_label


def test_mis_when_legs_carry_intraday_product() -> None:
    legs = {"protection": [], "income": [{"tradingsymbol": "SC", "product": "INTRADAY"}]}
    assert _option_product_label(json.dumps(legs)) == "MIS"


def test_mis_survives_every_leg_being_stopped_out() -> None:
    legs = {"protection": [], "income": [], "closed": [{"tradingsymbol": "SC", "product": "INTRADAY"}]}
    assert _option_product_label(legs) == "MIS"


def test_legacy_legs_without_product_are_nrml() -> None:
    assert _option_product_label({"income": [{"tradingsymbol": "SP"}]}) == "NRML"
    assert _option_product_label("not json") == "NRML"
