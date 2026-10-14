"""
JSON DSL parser — converts JSON strategy spec to USSSpec.

No LLM dependency. The JSON format is the canonical human-authored format.
Accepts both dict and raw JSON string.
"""

from __future__ import annotations

import json
from typing import Any

from ..uss.schema import (
    Condition,
    ConditionType,
    Direction,
    EntryConditions,
    ExitConditions,
    IntradaySession,
    LogicOperator,
    MarketFilter,
    Product,
    RegimeFilter,
    SLMethod,
    StopLoss,
    Target,
    TargetMethod,
    Timeframe,
    TrailingStop,
    TrailMethod,
    UniverseFilter,
    USSMeta,
    USSSpec,
)


class JSONParseError(ValueError):
    pass


class JSONParser:
    """Parses JSON DSL dict/string → USSSpec."""

    def parse(self, source: dict[str, Any] | str) -> USSSpec:
        data: dict[str, Any] = json.loads(source) if isinstance(source, str) else source

        try:
            meta = self._parse_meta(data.get("meta", {}))
            universe = self._parse_universe(data.get("universe_filter", {}))
            entry = self._parse_entry(data["entry_conditions"])
            sl = self._parse_sl(data["stop_loss"])
            target = self._parse_target(data["target"])
            exits = self._parse_exits(data.get("exit_conditions", {}))
            intraday = self._parse_intraday(data.get("intraday"))
            market_filter = self._parse_market_filter(data.get("market_filter"))
            regime_filter = self._parse_regime_filter(data.get("regime_filter"))
        except KeyError as exc:
            raise JSONParseError(f"Missing required field: {exc}") from exc

        risk_pct = data.get("risk_pct")

        return USSSpec(
            meta=meta,
            universe_filter=universe,
            entry_conditions=entry,
            stop_loss=sl,
            target=target,
            exit_conditions=exits,
            intraday=intraday,
            market_filter=market_filter,
            regime_filter=regime_filter,
            risk_pct=float(risk_pct) if risk_pct is not None else None,
            source_format="json",
        )

    def _parse_regime_filter(self, r: dict[str, Any] | None) -> RegimeFilter | None:
        if not r:
            return None
        return RegimeFilter(
            enabled=bool(r.get("enabled", True)),
            sma_period=int(r.get("sma_period", 200)),
            slope_lookback_bars=int(r.get("slope_lookback_bars", 20)),
            max_attempts_per_phase=int(r.get("max_attempts_per_phase", 3)),
        )

    def _parse_market_filter(self, m: dict[str, Any] | None) -> MarketFilter | None:
        if not m:
            return None
        return MarketFilter(
            symbol=m.get("symbol", "NIFTY 50"),
            enabled=bool(m.get("enabled", True)),
            mode=m.get("mode", "daily_direction"),
            sma_period=int(m.get("sma_period", 200)),
            min_breadth=float(m.get("min_breadth", 0.5)),
        )

    def _parse_intraday(self, s: dict[str, Any] | None) -> IntradaySession | None:
        if not s:
            return None
        return IntradaySession(
            range_window=s.get("range_window", "0915-0920"),
            entry_window=s.get("entry_window", "0920-1500"),
            exit_window=s.get("exit_window", "1510-1530"),
            timezone=s.get("timezone", "Asia/Kolkata"),
        )

    # ------------------------------------------------------------------
    def _parse_meta(self, m: dict[str, Any]) -> USSMeta:
        if not m.get("name"):
            raise JSONParseError("meta.name is required")
        return USSMeta(
            name=m["name"],
            display_name=m.get("display_name", ""),
            version=str(m.get("version", "1.0")),
            product=Product(m.get("product", "DELIVERY")),
            direction=Direction(m.get("direction", "LONG")),
            timeframe=Timeframe(m.get("timeframe", "DAILY")),
        )

    def _parse_universe(self, u: dict[str, Any]) -> UniverseFilter:
        return UniverseFilter(
            min_price=float(u.get("min_price", 0)),
            max_price=float(u["max_price"]) if "max_price" in u else None,
            min_adv_crores=float(u.get("min_adv_crores", 0)),
            max_market_cap_crores=(
                float(u["max_market_cap_crores"]) if "max_market_cap_crores" in u else None
            ),
            exchanges=u.get("exchanges", ["NSE"]),
            index_filter=u.get("index_filter"),
            symbols=u.get("symbols"),
        )

    def _parse_entry(self, e: dict[str, Any]) -> EntryConditions:
        logic = LogicOperator(e.get("logic", "AND"))
        raw_conds = e.get("conditions", [])
        if not raw_conds:
            raise JSONParseError("entry_conditions.conditions must not be empty")
        conditions = [self._parse_condition(c) for c in raw_conds]
        return EntryConditions(logic=logic, conditions=conditions)

    def _parse_condition(self, c: dict[str, Any]) -> Condition:
        ctype = ConditionType(c.get("type", "PRICE"))
        excluded = {"name", "type", "enabled"}
        params = {k: v for k, v in c.items() if k not in excluded}
        return Condition(
            name=c.get("name", ""),
            type=ctype,
            enabled=bool(c.get("enabled", True)),
            params=params,
        )

    def _parse_sl(self, s: dict[str, Any]) -> StopLoss:
        return StopLoss(
            method=SLMethod(s["method"]),
            reference=s.get("reference"),
            buffer_pct=float(s.get("buffer_pct", 0)),
            max_sl_pct=float(s.get("max_sl_pct", 10.0)),
            min_sl_pct=float(s.get("min_sl_pct", 0.0)),
            atr_period=s.get("atr_period"),
            atr_multiplier=s.get("atr_multiplier"),
            fixed_pct=s.get("fixed_pct"),
            fixed_points=s.get("fixed_points"),
            lookback_days=s.get("lookback_days"),
            range_fraction=s.get("range_fraction"),
        )

    def _parse_target(self, t: dict[str, Any]) -> Target:
        return Target(
            method=TargetMethod(t["method"]),
            ratio=t.get("ratio"),
            fixed_pct=t.get("fixed_pct"),
            fixed_points=t.get("fixed_points"),
            indicator=t.get("indicator"),
            period=t.get("period"),
        )

    def _parse_exits(self, e: dict[str, Any]) -> ExitConditions:
        trail: TrailingStop | None = None
        if "trailing_stop" in e:
            ts = e["trailing_stop"]
            trail = TrailingStop(
                method=TrailMethod(ts["method"]),
                indicator=ts.get("indicator"),
                period=ts.get("period"),
                cross=ts.get("cross"),
                atr_period=ts.get("atr_period"),
                atr_multiplier=ts.get("atr_multiplier"),
                chandelier_period=ts.get("chandelier_period"),
                chandelier_multiplier=ts.get("chandelier_multiplier"),
                fixed_pct=ts.get("fixed_pct"),
            )
        return ExitConditions(
            trailing_stop=trail,
            time_stop_days=e.get("time_stop_days"),
            breakeven_at_r=e.get("breakeven_at_r"),
            partial_book_r=e.get("partial_book_r"),
            trail_activation_r=e.get("trail_activation_r"),
            reversal_exit=bool(e.get("reversal_exit", False)),
            time_stop_before_activation=bool(e.get("time_stop_before_activation", False)),
            regime_flip_exit=bool(e.get("regime_flip_exit", False)),
            partial_fraction=float(e.get("partial_fraction", 0.5)),
            giveback_lock=e.get("giveback_lock"),
            time_decay=e.get("time_decay") or [],
        )
