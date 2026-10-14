"""
Corporate Action Handler -- split / bonus SL adjust.

When a stock splits or issues a bonus, open positions and their stop-loss
prices must be adjusted so the risk/reward ratio and monetary risk remain
unchanged.

Supported action types:
  SPLIT  -- e.g. 1:2 split: price halves, quantity doubles, SL halves
  BONUS  -- e.g. 1:1 bonus: quantity doubles, adjusted price halves, SL halves
  DIVIDEND -- no price adjustment needed; SL unchanged

The caller (pre-market scan / supervisor) is responsible for:
  - detecting the ex-date (from NSE corporate action API)
  - calling adjust_position() and writing updated values to the DB
  - placing a modified GTT order with the new SL

This module only does the arithmetic -- no DB, no broker calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ActionType(StrEnum):
    SPLIT = "SPLIT"
    BONUS = "BONUS"
    DIVIDEND = "DIVIDEND"
    RIGHTS = "RIGHTS"  # rights issue — flag for human review, block trading until settled


@dataclass
class CorporateAction:
    """A single corporate action event."""

    symbol: str
    action_type: ActionType
    ratio_numerator: int  # new shares for SPLIT/BONUS; e.g. 2 in 1:2 split
    ratio_denominator: int  # existing shares; e.g. 1 in 1:2 split
    ex_date: str  # ISO date string "YYYY-MM-DD"


@dataclass
class PositionAdjustment:
    """Result of applying a corporate action to an open position."""

    symbol: str
    old_quantity: int
    new_quantity: int
    old_avg_price: float
    new_avg_price: float
    old_stop_loss: float
    new_stop_loss: float
    old_target: float
    new_target: float
    adjustment_factor: float  # price_new = price_old / factor; qty_new = qty_old * factor
    action_applied: ActionType
    note: str


def adjust_position(
    symbol: str,
    quantity: int,
    avg_price: float,
    stop_loss: float,
    target: float,
    action: CorporateAction,
) -> PositionAdjustment:
    """
    Adjust an open position's quantity, average price, SL, and target for
    a corporate action.

    For SPLIT (e.g. 1:2):
      factor = ratio_numerator / ratio_denominator = 2
      new_qty = old_qty * factor
      new_price = old_price / factor
      new_sl = old_sl / factor
      new_target = old_target / factor

    For BONUS (e.g. 1:1 bonus = 1 new share per 1 existing):
      factor = (ratio_numerator + ratio_denominator) / ratio_denominator
      e.g. 1:1 bonus -> factor = 2
      Adjustment identical to split.

    For DIVIDEND:
      No adjustment needed; returns unchanged values.

    Args:
        symbol: Instrument symbol.
        quantity: Current held quantity.
        avg_price: Average entry price.
        stop_loss: Current SL price.
        target: Current target price.
        action: CorporateAction event.

    Returns:
        PositionAdjustment with all adjusted values.
    """
    if action.action_type == ActionType.RIGHTS:
        # Rights issue: complex — do NOT auto-adjust. Flag for human review and block trading.
        return PositionAdjustment(
            symbol=symbol,
            old_quantity=quantity,
            new_quantity=quantity,
            old_avg_price=avg_price,
            new_avg_price=avg_price,
            old_stop_loss=stop_loss,
            new_stop_loss=stop_loss,
            old_target=target,
            new_target=target,
            adjustment_factor=1.0,
            action_applied=ActionType.RIGHTS,
            note="RIGHTS_ISSUE: flag for human review; trading blocked until settled",
        )

    if action.action_type == ActionType.DIVIDEND:
        return PositionAdjustment(
            symbol=symbol,
            old_quantity=quantity,
            new_quantity=quantity,
            old_avg_price=avg_price,
            new_avg_price=avg_price,
            old_stop_loss=stop_loss,
            new_stop_loss=stop_loss,
            old_target=target,
            new_target=target,
            adjustment_factor=1.0,
            action_applied=ActionType.DIVIDEND,
            note="Dividend: no price/quantity adjustment required",
        )

    if action.action_type == ActionType.SPLIT:
        # 1:N split -> factor = N/1 = ratio_numerator / ratio_denominator
        factor = action.ratio_numerator / action.ratio_denominator
    else:
        # BONUS: 1:N bonus -> N new shares per 1 existing
        # factor = (existing + new) / existing
        factor = (action.ratio_denominator + action.ratio_numerator) / action.ratio_denominator

    new_quantity = round(quantity * factor)
    new_avg_price = round(avg_price / factor, 2)
    new_stop_loss = round(stop_loss / factor, 2)
    new_target = round(target / factor, 2)

    note = (
        f"{action.action_type.value} {action.ratio_numerator}:{action.ratio_denominator}"
        f" applied (factor {factor:.4f}). "
        f"Qty {quantity}->{new_quantity}, SL {stop_loss:.2f}->{new_stop_loss:.2f}"
    )

    return PositionAdjustment(
        symbol=symbol,
        old_quantity=quantity,
        new_quantity=new_quantity,
        old_avg_price=avg_price,
        new_avg_price=new_avg_price,
        old_stop_loss=stop_loss,
        new_stop_loss=new_stop_loss,
        old_target=target,
        new_target=new_target,
        adjustment_factor=round(factor, 6),
        action_applied=action.action_type,
        note=note,
    )


def get_rights_symbols(adjustments: list[PositionAdjustment]) -> set[str]:
    """Return symbols where a RIGHTS ISSUE was detected — caller should block trading these."""
    return {a.symbol for a in adjustments if a.action_applied == ActionType.RIGHTS}


class CorporateActionWatcher:
    """
    Watches for corporate actions affecting open positions.

    Reads from data/corporate_actions.json (operator-maintained).
    Call pending_for_today() pre-market to detect adjustments needed today.
    """

    def __init__(
        self,
        actions: list[CorporateAction] | None = None,
        data_path: str | None = None,
    ) -> None:
        if actions is not None:
            self._actions = list(actions)
        else:
            import json
            from pathlib import Path

            p = (
                Path(data_path)
                if data_path
                else Path(__file__).parent.parent.parent / "data" / "corporate_actions.json"
            )
            if p.exists():
                raw: list[dict[str, object]] = json.loads(p.read_text(encoding="utf-8"))
                self._actions = [
                    CorporateAction(
                        symbol=str(r["symbol"]),
                        action_type=ActionType(str(r["action_type"])),
                        ratio_numerator=int(str(r["ratio_numerator"])),
                        ratio_denominator=int(str(r["ratio_denominator"])),
                        ex_date=str(r["ex_date"]),
                    )
                    for r in raw
                ]
            else:
                self._actions = []

    def pending_for_today(
        self, symbols: set[str], today: str | None = None
    ) -> list[CorporateAction]:
        """Return actions whose ex_date is today (IST) and symbol is in the given set."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        if today is None:
            today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
        return [a for a in self._actions if a.symbol in symbols and a.ex_date == today]

    async def refresh(self, session: Any) -> bool:
        """
        Fetch corporate actions from NSE API and update data/corporate_actions.json.
        session: aiohttp.ClientSession. Returns True on success.
        Writes: data/corporate_actions.json — [{symbol, action_type, ratio_numerator, ratio_denominator, ex_date}]
        """
        import json as _json
        from datetime import datetime as _dt
        from datetime import timedelta as _td
        from pathlib import Path as _Path

        import aiohttp as _aiohttp

        _data_path = _Path(__file__).parent.parent.parent / "data" / "corporate_actions.json"
        _headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/",
        }
        try:
            async with session.get(
                "https://www.nseindia.com",
                headers={**_headers, "Accept": "text/html,application/xhtml+xml"},
                timeout=_aiohttp.ClientTimeout(total=15),
            ) as _:
                pass
            _today = _dt.now().strftime("%d-%m-%Y")
            _end = (_dt.now() + _td(days=30)).strftime("%d-%m-%Y")
            async with session.get(
                f"https://www.nseindia.com/api/corporates-corporateActions"
                f"?index=equities&from_date={_today}&to_date={_end}",
                headers=_headers,
                timeout=_aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json(content_type=None)
        except Exception:
            return False

        if not isinstance(data, list):
            return False

        _TYPE_MAP = {"split": "SPLIT", "bonus": "BONUS", "dividend": "DIVIDEND", "rights": "RIGHTS"}
        parsed: list[dict[str, Any]] = []
        for row in data:
            raw_type = str(row.get("purpose", "")).lower()
            action_type = next((v for k, v in _TYPE_MAP.items() if k in raw_type), None)
            if action_type is None:
                continue
            symbol = str(row.get("symbol", "")).strip().upper()
            ex_date_raw = str(row.get("exDate", "")).strip()
            if not symbol or not ex_date_raw:
                continue
            ex_date = None
            for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
                try:
                    ex_date = _dt.strptime(ex_date_raw[:11], fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
            if ex_date is None:
                continue
            raw_ratio = str(row.get("ratio", "1:1"))
            parts = raw_ratio.split(":") if ":" in raw_ratio else ["1", "1"]
            try:
                num, den = int(parts[0].strip()), int(parts[1].strip())
            except (ValueError, IndexError):
                num, den = 1, 1
            parsed.append(
                {
                    "symbol": symbol,
                    "action_type": action_type,
                    "ratio_numerator": num,
                    "ratio_denominator": den,
                    "ex_date": ex_date,
                }
            )

        if parsed:
            _data_path.parent.mkdir(parents=True, exist_ok=True)
            _data_path.write_text(_json.dumps(parsed, indent=2), encoding="utf-8")
            self._actions = [
                CorporateAction(
                    symbol=r["symbol"],
                    action_type=ActionType(r["action_type"]),
                    ratio_numerator=r["ratio_numerator"],
                    ratio_denominator=r["ratio_denominator"],
                    ex_date=r["ex_date"],
                )
                for r in parsed
            ]
        return bool(parsed)

    def adjustments_for_positions(
        self,
        positions: list[tuple[str, int, float, float, float]],
        today: str | None = None,
    ) -> list[PositionAdjustment]:
        """
        Return PositionAdjustment for each position that has a pending action today.

        positions: list of (symbol, quantity, avg_price, stop_loss, target)
        """
        symbols = {p[0] for p in positions}
        pending = self.pending_for_today(symbols, today)
        if not pending:
            return []
        action_map = {a.symbol: a for a in pending}
        return [
            adjust_position(sym, qty, avg_p, sl, tgt, action_map[sym])
            for sym, qty, avg_p, sl, tgt in positions
            if sym in action_map
        ]
