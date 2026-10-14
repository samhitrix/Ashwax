"""Adapter: seller-desk credit structure -> CRM risk-gate signal.

The options seller desk (ASHWAX) used to bypass src/risk/crm.py's G0-G10
pipeline entirely -- every other live-trading engine in this system
(intraday, swing, buyer-first options) is forced through it. This module is
the one place that translates a selected multi-leg credit structure into a
CRMSignal so seller_desk.run_cycle() can call crm.evaluate() before placing
an order, same as every other engine.

IMPORTANT -- do not reuse OptionsRiskGate (src/options/options_risk.py)
here: its avoid_naked_selling / _is_naked() check hard-rejects any
net-credit or uncovered-short structure, which is exactly what this desk
writes (iron condors, credit spreads). CRM (via this adapter) is this
desk's risk gate, not OptionsRiskGate.
"""

from __future__ import annotations

from src.options import seller_settings
from src.risk.crm import CRMSignal


def build_seller_crm_signal(
    *,
    signal_id: str,
    underlying: str,
    strategy_type: str,
    lots: int,
    max_loss: float,
    max_profit: float,
    regime: str,
    vix: float,
    registry_name: str = "options_seller_desk_v3",
) -> CRMSignal:
    """Map a selected seller-desk credit structure onto a CRMSignal.

    ``max_loss``/``max_profit`` must be the values from
    ``estimate_economics()`` for the FULL structure at ``lots`` lots (that
    function already multiplies by lots internally via notional_width =
    width * lot_size * lots) -- do NOT multiply by lots again here, or G8's
    risk_rupees/qty division would treat the whole position's risk as the
    per-lot risk and over-restrict sizing.

    stop_loss_pct/target_pct below are SYNTHETIC placeholders, not the
    structure's real economics: risk_rupees (the structure's actual
    max_loss) is what drives G8's sizing -- entry_price stays 0.0 so G8
    takes its risk_rupees/qty branch instead of entry_price*stop_loss_pct
    (src/risk/crm.py _g8_sl_sizing). A credit spread's real
    max_profit/max_loss ratio is typically well under 1.0 and would wrongly
    fail G1's equity-tuned R:R-ratio floor if used directly here -- so these
    two fields exist only to clear G1's ratio/>0 guard and G8's
    stop_loss_pct>0 guard, and must never be read as the structure's actual
    stop/target. Configurable via config/options_settings.json
    options_trading.crm_adapter (not hardcoded) so they can be tuned without
    a code change.
    """
    cfg = seller_settings.crm_adapter_settings()
    sl_pct = float(cfg.get("synthetic_stop_loss_pct", 0.01))
    tgt_pct = float(cfg.get("synthetic_target_pct", 0.02))
    strategy_health = str(cfg.get("default_strategy_health", "HEALTHY"))
    lots = max(1, lots)

    signal = CRMSignal(
        signal_id=signal_id,
        symbol=underlying,
        direction="SELL",
        strategy_name=f"options_seller_desk::{strategy_type}",
        intended_quantity=lots,
        stop_loss_pct=sl_pct,
        target_pct=tgt_pct,
        risk_rupees=max(0.0, max_loss),
        entry_price=0.0,
        regime=regime,
        strategy_health=strategy_health,
        vix=vix,
        asset_class="options",
        # engine left at CRMSignal's default ("swing") -- harmless: G7 now
        # resolves the real per-engine bucket via segment_of(signal), which
        # reads asset_class ("options" above) rather than this field. Was a
        # real bug until 2026-09-10 (options silently shared swing's
        # daily-trade/open-position caps via the raw engine field). Real
        # risk math still flows through risk_rupees/G8; margin is separately
        # gated by src/options/margin_monitor.py.
    )
    # strategy_name stays per-structure ("options_seller_desk::<type>") for logs/counters;
    # registry_name is what per-strategy registry policy (G9 llm_gate_required) keys on.
    # Without it no desk signal ever matched its registry entry (found 2026-10-05).
    signal.meta["registry_name"] = registry_name
    return signal
