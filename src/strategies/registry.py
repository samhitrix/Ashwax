"""
StrategyRegistry â€” loads, validates, backtests, and serves CompiledStrategy objects.

Hot-reload: call reload() to pick up new/changed strategy files without restart.
Workers call get(name) to obtain the compiled strategy for live evaluation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from src.infrastructure.logger import get_logger

from .backtest.engine import BacktestEngine, BacktestResult
from .parsers.json_parser import JSONParseError, JSONParser
from .uss.compiler import CompiledStrategy, USSCompiler
from .uss.schema import USSSpec
from .uss.validator import USSValidator

logger = get_logger(__name__)

_STRATEGY_DIRS = [
    Path("strategies/builtin"),
    Path("strategies/agent_created"),
]


class RegistrationError(ValueError):
    pass


class StrategyRegistry:
    """
    Thread-safe read / single-writer strategy store.

    get(name)   â†’ CompiledStrategy | None
    reload()    â†’ rescans disk, compiles new/changed files, removes deleted
    list()      â†’ iterator over registered names
    register()  â†’ programmatic registration (skips disk scan, runs validation + backtest)
    """

    def __init__(
        self,
        validator: USSValidator | None = None,
        compiler: USSCompiler | None = None,
        backtest_engine: BacktestEngine | None = None,
        strategy_dirs: list[Path] | None = None,
    ) -> None:
        self._validator = validator or USSValidator()
        self._compiler = compiler or USSCompiler()
        self._backtest = backtest_engine or BacktestEngine()
        self._dirs = strategy_dirs or _STRATEGY_DIRS
        self._strategies: dict[str, CompiledStrategy] = {}
        self._backtest_results: dict[str, BacktestResult] = {}
        self._backtest_computed_at: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, name: str) -> CompiledStrategy | None:
        return self._strategies.get(name)

    def spec_for_open_positions(self, name: str) -> CompiledStrategy | None:
        """Compiled strategy for MANAGING positions it already holds, even when it is disabled.

        get() only knows enabled strategies, so a disabled strategy's open positions lost
        their exit management (no trail, no time stop) after any restart -- RADICO sat
        69 days under oneil_canslim (disabled 2026-09-11) with only its resting stop
        (found 2026-10-06). Disabling a strategy stops NEW entries; it must never abandon
        capital already at risk. The result is NOT registered, so it can never originate
        a signal. None only when no spec file with that name exists."""
        hit = self._strategies.get(name)
        if hit is not None:
            return hit
        for directory in self._dirs:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if data.get("meta", {}).get("name", path.stem) != name:
                        continue
                    spec = self._maybe_apply_promoted_overrides(name, JSONParser().parse(data))
                    return self._compiler.compile(spec)
                except (JSONParseError, ValueError, OSError) as exc:
                    logger.warning("spec_for_open_positions_failed name=%s err=%s", name, exc)
                    return None
        return None

    def names(self) -> Iterator[str]:
        return iter(list(self._strategies))

    def all_enabled(self) -> list[CompiledStrategy]:
        return [s for s in self._strategies.values() if s.spec.enabled]

    def backtest_result(self, name: str) -> BacktestResult | None:
        return self._backtest_results.get(name)

    def backtest_computed_at(self, name: str) -> datetime | None:
        """When `name`'s current BacktestResult was computed — feeds the Signal
        Quality Score's recency_relevance component (src/execution/signal_quality_adapter.py)."""
        return self._backtest_computed_at.get(name)

    def _load_promoted_overrides(self, name: str) -> dict[str, object] | None:
        """config/strategy_{name}.json, written by PromotionEngine.promote()
        (src/learning/promotion.py) — None if no such file exists."""
        path = Path("config") / f"strategy_{name}.json"
        if not path.exists():
            return None
        try:
            data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read promoted overrides for %s: %s", name, exc)
            return None

    def _maybe_apply_promoted_overrides(self, name: str, spec: USSSpec) -> USSSpec:
        """Apply a promoted-config override to `spec`, gated by config/learning.json
        apply_promoted_overrides.mode: "off" = ignore any override file even if
        present (today's behavior — no file is ever read back into a live spec).
        "shadow" = log that an override file exists and what it would change,
        without applying it. "live" = actually apply it via
        src.strategies.evolution.apply_param_overrides()."""
        overrides = self._load_promoted_overrides(name)
        if overrides is None:
            return spec
        mode = self._apply_promoted_overrides_mode()
        if mode == "off":
            return spec
        if mode == "shadow":
            logger.info(
                "promoted_override_shadow strategy=%s would_apply=%s (mode=shadow, not applied)",
                name,
                overrides,
            )
            return spec
        from .evolution import apply_param_overrides

        numeric_overrides: dict[str, float] = {}
        for k, v in overrides.items():
            if isinstance(v, int | float | str):
                try:
                    numeric_overrides[k] = float(v)
                    continue
                except ValueError:
                    pass
            logger.warning(
                "promoted_override_non_numeric strategy=%s key=%s value=%r — skipped", name, k, v
            )
        logger.info("promoted_override_applied strategy=%s overrides=%s", name, numeric_overrides)
        return apply_param_overrides(spec, numeric_overrides)

    @staticmethod
    def _apply_promoted_overrides_mode() -> str:
        try:
            cfg_path = Path("config/learning.json")
            if not cfg_path.exists():
                return "off"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            return str(cfg.get("apply_promoted_overrides", {}).get("mode") or "off")
        except Exception:
            return "off"

    def register(
        self,
        spec_dict: dict[str, object],
        df: pd.DataFrame | None = None,
        skip_backtest: bool = False,
    ) -> CompiledStrategy:
        """
        Parse, validate, optionally backtest, and register a strategy.

        df       required when skip_backtest=False.
        skip_backtest=True bypasses the backtest gate (tests / paper mode only).

        A failing backtest no longer hard-deregisters the strategy (that would
        block every future signal from it, including a live setup that's a clean,
        high-quality breakout). Instead it stays registered — pass/fail is tracked
        via backtest_result(name) — and downstream CRM Gate 6 (strategy_health)
        gates individual signals from a backtest-failing strategy on live setup
        quality instead, sized down when it clears the bar.
        """
        parser = JSONParser()
        try:
            spec = parser.parse(spec_dict)
        except (JSONParseError, ValueError) as exc:
            raise RegistrationError(f"Parse failed: {exc}") from exc

        spec = self._maybe_apply_promoted_overrides(spec.meta.name, spec)
        result = self._validator.validate(spec)
        if not result.ok:
            raise RegistrationError(f"Validation failed: {result.errors}")

        for w in result.warnings:
            logger.warning("Strategy %s: %s", spec.meta.name, w)

        compiled = self._compiler.compile(spec)
        self._backtest_and_register(spec.meta.name, compiled, df, skip_backtest)
        return compiled

    def register_spec(
        self,
        spec: USSSpec,
        df: pd.DataFrame | None = None,
        skip_backtest: bool = False,
    ) -> CompiledStrategy:
        """Register a pre-parsed USSSpec directly (skips JSONParser)."""
        spec = self._maybe_apply_promoted_overrides(spec.meta.name, spec)
        result = self._validator.validate(spec)
        if not result.ok:
            raise RegistrationError(f"Validation failed: {result.errors}")

        for w in result.warnings:
            logger.warning("Strategy %s: %s", spec.meta.name, w)

        compiled = self._compiler.compile(spec)
        self._backtest_and_register(spec.meta.name, compiled, df, skip_backtest)
        return compiled

    def _backtest_and_register(
        self,
        name: str,
        compiled: CompiledStrategy,
        df: pd.DataFrame | None,
        skip_backtest: bool,
    ) -> None:
        if not skip_backtest:
            if df is None:
                raise RegistrationError("df required for backtest")
            bt = self._backtest.run(compiled, df)
            self._backtest_results[name] = bt
            self._backtest_computed_at[name] = datetime.now(UTC)
            if not bt.passed:
                logger.warning(
                    "Strategy %s FAILED backtest (%s) — registered; live signals now "
                    "gated on setup quality (CRM G6) instead of being fully blocked.",
                    name,
                    bt.reject_reason,
                )
            else:
                logger.info("Strategy %s registered. %s", name, bt.summary())
        else:
            logger.info("Strategy %s registered (backtest skipped).", name)

        self._strategies[name] = compiled

    def reload(self, df: pd.DataFrame | None = None) -> None:
        """Rescan strategy directories. Adds new, removes deleted, recompiles changed."""
        found: set[str] = set()

        # Load enabled/disabled flags from the central registry file
        _registry_path = Path("config/strategies_registry.json")
        _registry_enabled: dict[str, bool] = {}
        if _registry_path.exists():
            try:
                _reg = json.loads(_registry_path.read_text(encoding="utf-8"))
                for entry in _reg.get("strategies", []):
                    _registry_enabled[entry["name"]] = bool(entry.get("enabled", True))
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Could not read strategies_registry.json: %s", exc)

        for directory in self._dirs:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    name = data.get("meta", {}).get("name", path.stem)
                    if not _registry_enabled.get(name, True):
                        logger.info(
                            "Strategy %s skipped (disabled in strategies_registry.json).", name
                        )
                        continue  # not added to found → removed from _strategies on next reload
                    found.add(name)
                    # Always recompile — picks up JSON edits and compiler changes
                    self.register(data, df=df, skip_backtest=(df is None))
                except (RegistrationError, json.JSONDecodeError) as exc:
                    logger.warning("Skipping %s: %s", path.name, exc)

        # Remove deleted strategies
        removed = set(self._strategies) - found
        for name in removed:
            del self._strategies[name]
            self._backtest_results.pop(name, None)
            self._backtest_computed_at.pop(name, None)
            logger.info("Strategy %s removed (file deleted).", name)

    async def persist_to_db(self, pool: object) -> None:
        """Upsert all loaded strategies into strategy_registry_db table."""
        for name, compiled in self._strategies.items():
            spec = compiled.spec
            bt = self._backtest_results.get(name)
            source = "agent_created"
            for d in self._dirs:
                if (d / f"{name}.json").exists():
                    source = d.name
                    break
            meta = getattr(spec, "meta", spec)
            is_active = getattr(meta, "enabled", True)
            try:
                async with pool.acquire() as conn:  # type: ignore[attr-defined]
                    await conn.execute(
                        """
                        INSERT INTO strategy_registry_db
                            (strategy_name, source, is_active,
                             backtest_wr, backtest_pf, backtest_expectancy,
                             backtest_trade_count, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                        ON CONFLICT (strategy_name) DO UPDATE
                            SET source=EXCLUDED.source,
                                is_active=EXCLUDED.is_active,
                                backtest_wr=EXCLUDED.backtest_wr,
                                backtest_pf=EXCLUDED.backtest_pf,
                                backtest_expectancy=EXCLUDED.backtest_expectancy,
                                backtest_trade_count=EXCLUDED.backtest_trade_count,
                                updated_at=NOW()
                        """,
                        name,
                        source,
                        bool(is_active),
                        float(bt.win_rate) if bt and hasattr(bt, "win_rate") else None,
                        float(bt.profit_factor) if bt and hasattr(bt, "profit_factor") else None,
                        float(bt.expectancy_r) if bt and hasattr(bt, "expectancy_r") else None,
                        int(bt.trade_count) if bt and hasattr(bt, "trade_count") else None,
                    )
            except Exception as exc:
                logger.warning("strategy_registry_db upsert failed %s: %s", name, exc)
