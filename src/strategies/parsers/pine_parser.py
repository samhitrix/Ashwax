"""
Pine Script parser — extracts strategy spec from Pine Script source → USSSpec.

Uses regex-based heuristics. Pine Script is Turing-complete so this is
necessarily approximate: it recognises common patterns (ATR-based SL,
recent-low SL, fixed-pct SL, R:R targets, SMA/EMA/RSI/crossover conditions)
and produces a best-effort USSSpec.

Sets source_format="pine" on the resulting spec.
Raises PineParseError when the source cannot be mapped to a valid USSSpec.
"""

from __future__ import annotations

import re

from ..uss.schema import (
    Condition,
    ConditionType,
    Direction,
    EntryConditions,
    ExitConditions,
    LogicOperator,
    Product,
    SLMethod,
    StopLoss,
    Target,
    TargetMethod,
    Timeframe,
    UniverseFilter,
    USSMeta,
    USSSpec,
)

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_RE_NAME = re.compile(r'(?:strategy|indicator)\s*\(\s*"([^"]+)"')
_RE_TF = re.compile(r'timeframe\.period\s*==\s*"([^"]+)"')
_RE_LONG = re.compile(r"strategy\.entry\s*\([^)]*strategy\.long")
_RE_SHORT = re.compile(r"strategy\.entry\s*\([^)]*strategy\.short")
_RE_ATR = re.compile(r"ta\.atr\s*\(\s*(\d+)\s*\)\s*\*\s*([\d.]+)")
_RE_RECENT_LOW = re.compile(r"ta\.lowest\s*\([^,)]+,\s*(\d+)\s*\)")
_RE_RECENT_HIGH = re.compile(r"ta\.highest\s*\([^,)]+,\s*(\d+)\s*\)")
_RE_SL_PCT = re.compile(r"(?:stop|sl)\s*=\s*close\s*\*\s*([\d.]+)")
_RE_EXIT_LIMIT = re.compile(r"strategy\.exit\s*\([^)]*\blimit\s*=")
_RE_RR = re.compile(r"(?:rr|r_r|risk_reward|ratio)\s*[=:]\s*([\d.]+)")
_RE_SMA = re.compile(r"(?:ta\.sma|sma)\s*\(\s*\w+\s*,\s*(\d+)\s*\)")
_RE_EMA = re.compile(r"(?:ta\.ema|ema)\s*\(\s*\w+\s*,\s*(\d+)\s*\)")
_RE_RSI = re.compile(r"(?:ta\.rsi|rsi)\s*\(\s*\w+\s*,\s*(\d+)\s*\)")
_RE_CROSSOVER = re.compile(r"ta\.crossover\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)")
_RE_CROSSUNDER = re.compile(r"ta\.crossunder\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)")

_TF_MAP: dict[str, Timeframe] = {
    "D": Timeframe.DAILY,
    "W": Timeframe.WEEKLY,
    "60": Timeframe.HOURLY,
    "30": Timeframe.THIRTY_MIN,
    "15": Timeframe.FIFTEEN_MIN,
    "5": Timeframe.FIVE_MIN,
    "1": Timeframe.ONE_MIN,
}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class PineParseError(ValueError):
    pass


class PineParser:
    """Parses Pine Script source → USSSpec via regex heuristics."""

    def parse(self, source: str) -> USSSpec:
        if not source.strip():
            raise PineParseError("Pine Script source cannot be empty")

        name = self._extract_name(source)
        direction = self._extract_direction(source)
        timeframe = self._extract_timeframe(source)
        sl = self._extract_sl(source)
        target = self._extract_target(source)
        conditions = self._extract_conditions(source)

        return USSSpec(
            meta=USSMeta(
                name=name,
                product=Product.DELIVERY,
                direction=direction,
                timeframe=timeframe,
            ),
            universe_filter=UniverseFilter(),
            entry_conditions=EntryConditions(logic=LogicOperator.AND, conditions=conditions),
            stop_loss=sl,
            target=target,
            exit_conditions=ExitConditions(),
            source_format="pine",
        )

    # ------------------------------------------------------------------

    def _extract_name(self, source: str) -> str:
        m = _RE_NAME.search(source)
        if not m:
            raise PineParseError("Cannot find strategy() or indicator() declaration")
        return m.group(1).lower().replace(" ", "_")

    def _extract_direction(self, source: str) -> Direction:
        has_short = bool(_RE_SHORT.search(source))
        has_long = bool(_RE_LONG.search(source))
        if has_short and not has_long:
            return Direction.SHORT
        return Direction.LONG

    def _extract_timeframe(self, source: str) -> Timeframe:
        m = _RE_TF.search(source)
        if m:
            return _TF_MAP.get(m.group(1), Timeframe.DAILY)
        return Timeframe.DAILY

    def _extract_sl(self, source: str) -> StopLoss:
        m_atr = _RE_ATR.search(source)
        if m_atr:
            return StopLoss(
                method=SLMethod.ATR_BASED,
                atr_period=int(m_atr.group(1)),
                atr_multiplier=float(m_atr.group(2)),
                max_sl_pct=10.0,
            )

        m_low = _RE_RECENT_LOW.search(source)
        if m_low:
            return StopLoss(
                method=SLMethod.RECENT_LOW,
                lookback_days=int(m_low.group(1)),
                buffer_pct=0.5,
                max_sl_pct=10.0,
            )

        m_high = _RE_RECENT_HIGH.search(source)
        if m_high:
            return StopLoss(
                method=SLMethod.RECENT_HIGH,
                lookback_days=int(m_high.group(1)),
                buffer_pct=0.5,
                max_sl_pct=10.0,
            )

        m_pct = _RE_SL_PCT.search(source)
        if m_pct:
            factor = float(m_pct.group(1))
            pct = round(abs(1.0 - factor) * 100.0, 4)
            return StopLoss(method=SLMethod.FIXED_PCT, fixed_pct=pct, max_sl_pct=10.0)

        return StopLoss(method=SLMethod.FIXED_PCT, fixed_pct=5.0, max_sl_pct=10.0)

    def _extract_target(self, source: str) -> Target:
        m_rr = _RE_RR.search(source)
        if m_rr:
            return Target(method=TargetMethod.RISK_REWARD, ratio=float(m_rr.group(1)))

        if _RE_EXIT_LIMIT.search(source):
            return Target(method=TargetMethod.RISK_REWARD, ratio=2.0)

        return Target(method=TargetMethod.RISK_REWARD, ratio=2.0)

    def _extract_conditions(self, source: str) -> list[Condition]:
        conditions: list[Condition] = []
        seen: set[str] = set()

        def _add(name: str, ctype: ConditionType, params: dict[str, object]) -> None:
            if name not in seen:
                seen.add(name)
                conditions.append(Condition(name=name, type=ctype, params=params))

        for m in _RE_SMA.finditer(source):
            _add(
                f"sma_{m.group(1)}",
                ConditionType.INDICATOR,
                {"indicator": "SMA", "period": int(m.group(1)), "condition": "price_above"},
            )

        for m in _RE_EMA.finditer(source):
            _add(
                f"ema_{m.group(1)}",
                ConditionType.INDICATOR,
                {"indicator": "EMA", "period": int(m.group(1)), "condition": "price_above"},
            )

        for m in _RE_RSI.finditer(source):
            _add(
                f"rsi_{m.group(1)}",
                ConditionType.INDICATOR,
                {"indicator": "RSI", "period": int(m.group(1))},
            )

        for m in _RE_CROSSOVER.finditer(source):
            _add(
                f"crossover_{m.group(1)}_{m.group(2)}",
                ConditionType.INDICATOR,
                {"condition": "crossover", "fast": m.group(1), "slow": m.group(2)},
            )

        for m in _RE_CROSSUNDER.finditer(source):
            _add(
                f"crossunder_{m.group(1)}_{m.group(2)}",
                ConditionType.INDICATOR,
                {"condition": "crossunder", "fast": m.group(1), "slow": m.group(2)},
            )

        if not conditions:
            conditions.append(
                Condition(
                    name="pine_entry",
                    type=ConditionType.PRICE,
                    params={"condition": "price_above", "reference": "open"},
                )
            )

        return conditions
