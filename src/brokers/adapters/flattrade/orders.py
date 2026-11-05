"""
Flattrade order pricing and enablement helpers (pure, no I/O except the config read).

Facts (docs/superpowers/specs/2026-10-15-phase0-broker-api-findings.md A1, A3, C-4, C-6):
  - PlaceOrder `prctyp` is LMT / SL-LMT only for API orders; MKT / SL-MKT are treated
    as blocked, so MARKET becomes a marketable LMT and SL-M an SL-LMT, both priced
    with a protection band (`market_protection_band_pct`, config/execution.json).
  - Numbers go over the wire as strings; prices are rupees.
  - The account's enabled price types / products / exchanges come from
    `UserDetails` (`orarr`, `prarr`, `exarr`).

Prices use Decimal so a tick grid never drifts (2513.8065 -> 2513.85, not
2513.8500000001).
"""

from __future__ import annotations

import json
import logging
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from src.brokers.adapters.flattrade.client import FlattradeError

_log = logging.getLogger(__name__)

EXECUTION_CONFIG = Path(__file__).resolve().parents[4] / "config" / "execution.json"

# Risk parameter: how far beyond LTP (MARKET) or the trigger (SL-M) the limit price
# may sit. A FRACTION: 0.005 = 0.5%. Wider fills more reliably in a fast market but
# allows more slippage; narrower risks no fill (the order then rests as a limit).
DEFAULT_PROTECTION_BAND = 0.005
# Above 5% a typo (e.g. 0.5 meaning "0.5%") would allow a 50% slippage band.
MAX_PROTECTION_BAND = 0.05

DEFAULT_TICK = Decimal("0.05")
_FLOAT_NOISE = Decimal("0.00000001")

# Canonical products plus the Kite-style aliases callers also pass (order_lifecycle
# treats "MIS" as intraday): MIS -> I, CNC -> C, NRML -> M (findings A1 prd table).
PRODUCT_TO_FT: dict[str, str] = {
    "INTRADAY": "I",
    "DELIVERY": "C",
    "MARGIN": "M",
    "MIS": "I",
    "CNC": "C",
    "NRML": "M",
}
SIDE_TO_FT: dict[str, str] = {"BUY": "B", "SELL": "S"}
VALIDITIES: frozenset[str] = frozenset({"DAY", "IOC"})
# canonical order_type -> Flattrade prctyp actually sent
ORDER_TYPE_TO_PRCTYP: dict[str, str] = {
    "LIMIT": "LMT",
    "MARKET": "LMT",
    "SL": "SL-LMT",
    "SL-M": "SL-LMT",
}
REMARKS_MAX = 20  # mirrors Angel's ordertag cap; Flattrade's own limit is undocumented


def round_to_tick(price: float | Decimal, tick: Decimal, mode: str) -> Decimal:
    """Snap `price` onto the `tick` grid: mode "up", "down" or "nearest" (half up)."""
    if tick <= 0:
        raise ValueError(f"tick must be positive, got {tick}")
    rounding = {"up": ROUND_CEILING, "down": ROUND_FLOOR, "nearest": ROUND_HALF_UP}[mode]
    # Drop float noise first (0.1 + 0.2 = 0.30000000000000004 must not ceil to 0.31).
    value = Decimal(str(price)).quantize(_FLOAT_NOISE, rounding=ROUND_HALF_UP)
    steps = (value / tick).to_integral_value(rounding=rounding)
    return (steps * tick).quantize(tick)


def fmt(price: Decimal) -> str:
    """Price as the wire string, at least 2 decimals ("2500.00"); finer ticks keep theirs."""
    exponent = price.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        return f"{price:f}"
    return f"{price.quantize(Decimal('0.01')):f}"


def parse_tick(value: Any) -> Decimal | None:
    """Quote `ti` -> positive Decimal, or None when absent / unusable."""
    try:
        tick = Decimal(str(value).strip())
    except (ArithmeticError, ValueError):
        return None
    return tick if tick.is_finite() and tick > 0 else None


def validate_band(value: Any) -> float:
    """Return the band as a float or raise ValueError (0 < band <= MAX_PROTECTION_BAND)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"market_protection_band_pct must be a number, got {value!r}")
    band = float(value)
    if not 0 < band <= MAX_PROTECTION_BAND:
        raise ValueError(
            f"market_protection_band_pct={band} out of range (0, {MAX_PROTECTION_BAND}]; "
            "it is a fraction: 0.005 = 0.5%"
        )
    return band


def load_protection_band(path: Path = EXECUTION_CONFIG) -> float:
    """`flattrade.market_protection_band_pct` from config/execution.json.

    A missing file or key gives DEFAULT_PROTECTION_BAND; an unreadable file or an
    invalid value raises ValueError (a risk parameter must never be silently wrong).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _log.warning(
            "flattrade_band_config_missing path=%s using default=%s", path, DEFAULT_PROTECTION_BAND
        )
        return DEFAULT_PROTECTION_BAND
    try:
        cfg = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path}: not valid JSON ({exc})") from exc
    block = cfg.get("flattrade") if isinstance(cfg, dict) else None
    if not isinstance(block, dict) or "market_protection_band_pct" not in block:
        return DEFAULT_PROTECTION_BAND
    return validate_band(block["market_protection_band_pct"])


def check_enabled(details: dict[str, Any], prctyp: str, prd: str, exch: str) -> None:
    """Fail loudly unless UserDetails says this account may send (prctyp, prd, exch).

    Shapes (findings A1 / A3 list the keys, not the element shapes; UNVERIFIED):
    `orarr` a list of price-type strings; `prarr` a list of objects with `prd` and an
    optional `exch` list (bare strings are accepted as product codes); `exarr` a list
    of exchange strings, checked only when present.
    """
    orarr = details.get("orarr")
    if not isinstance(orarr, list) or not all(isinstance(x, str) for x in orarr):
        raise FlattradeError(f"UserDetails: orarr missing or unexpected shape ({orarr!r})")
    if prctyp not in {x.strip().upper() for x in orarr}:
        raise FlattradeError(
            f"Flattrade account is not enabled for price type {prctyp} (UserDetails orarr={orarr})"
        )

    prarr = details.get("prarr")
    if not isinstance(prarr, list):
        raise FlattradeError(f"UserDetails: prarr missing or unexpected shape ({prarr!r})")
    product_exchanges: dict[str, list[str] | None] = {}
    for entry in prarr:
        if isinstance(entry, str):
            product_exchanges[entry.strip().upper()] = None
        elif isinstance(entry, dict) and isinstance(entry.get("prd"), str):
            exchs = entry.get("exch")
            product_exchanges[entry["prd"].strip().upper()] = (
                [str(e).upper() for e in exchs] if isinstance(exchs, list) else None
            )
        else:
            raise FlattradeError(f"UserDetails: prarr entry has unexpected shape ({entry!r})")
    if prd not in product_exchanges:
        raise FlattradeError(
            f"Flattrade account is not enabled for product {prd} (UserDetails prarr)"
        )
    allowed = product_exchanges[prd]
    if allowed is not None and exch not in allowed:
        raise FlattradeError(
            f"Flattrade product {prd} is not enabled on {exch} (UserDetails prarr: {allowed})"
        )

    exarr = details.get("exarr")
    if isinstance(exarr, list) and exch not in {str(e).upper() for e in exarr}:
        raise FlattradeError(f"Flattrade account is not enabled on {exch} (UserDetails exarr)")
