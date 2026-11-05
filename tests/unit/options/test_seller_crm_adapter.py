"""Unit tests for src/options/seller_crm_adapter.py -- the field mapping from
a selected seller-desk credit structure onto a CRMSignal, including the
synthetic stop_loss_pct/target_pct placeholder math that must satisfy G1's
R:R-ratio floor and G8's stop_loss_pct>0 guard without ever being read as the
structure's real economics."""

from __future__ import annotations

from src.options import seller_settings
from src.options.seller_crm_adapter import build_seller_crm_signal


def _signal(**overrides):  # type: ignore[no-untyped-def]
    base = dict(
        signal_id="SELLER-TEST",
        underlying="NSE:NIFTY",
        strategy_type="iron_condor",
        lots=3,
        max_loss=15_000.0,
        max_profit=5_000.0,
        regime="HIGH_VOL_RANGE_BOUND",
        vix=18.5,
    )
    base.update(overrides)
    return build_seller_crm_signal(**base)


def test_risk_rupees_is_structure_max_loss_not_multiplied_by_lots_again() -> None:
    # estimate_economics() already scales max_loss by lots internally
    # (notional_width = width * lot_size * lots) -- risk_rupees must be
    # exactly that figure, not max_loss * lots again.
    sig = _signal(max_loss=15_000.0, lots=3)
    assert sig.risk_rupees == 15_000.0


def test_intended_quantity_is_lots() -> None:
    sig = _signal(lots=4)
    assert sig.intended_quantity == 4


def test_lots_floored_at_one() -> None:
    sig = _signal(lots=0)
    assert sig.intended_quantity == 1


def test_entry_price_is_zero_forcing_g8_risk_rupees_branch() -> None:
    sig = _signal()
    assert sig.entry_price == 0.0


def test_stop_loss_and_target_pct_are_positive_and_clear_default_rr_floor() -> None:
    sig = _signal()
    assert sig.stop_loss_pct > 0.0
    assert sig.target_pct > 0.0
    # G1's default min_risk_reward_ratio is 1.8 (operator.json) -- these
    # synthetic placeholders must clear it even though they carry none of
    # the structure's real economics.
    assert sig.target_pct / sig.stop_loss_pct >= 1.8


def test_strategy_name_is_namespaced_per_structure_type() -> None:
    sig = _signal(strategy_type="bull_put_spread")
    assert sig.strategy_name == "options_seller_desk::bull_put_spread"


def test_asset_class_is_options() -> None:
    assert _signal().asset_class == "options"


def test_direction_and_regime_and_vix_pass_through() -> None:
    sig = _signal(regime="LOW_VOL_RANGE_BOUND", vix=22.0)
    assert sig.direction == "SELL"
    assert sig.regime == "LOW_VOL_RANGE_BOUND"
    assert sig.vix == 22.0


def test_strategy_health_defaults_to_healthy() -> None:
    assert _signal().strategy_health == "HEALTHY"


def test_config_overrides_are_honored(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        seller_settings,
        "crm_adapter_settings",
        lambda settings=None: {
            "synthetic_stop_loss_pct": 0.05,
            "synthetic_target_pct": 0.09,
            "default_strategy_health": "DEGRADED",
        },
    )
    sig = _signal()
    assert sig.stop_loss_pct == 0.05
    assert sig.target_pct == 0.09
    assert sig.strategy_health == "DEGRADED"
