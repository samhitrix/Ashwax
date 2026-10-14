"""
Strategy Discovery Engine â€” scans directories for strategy files and registers
them through the parse â†’ validate â†’ backtest â†’ registry pipeline.

Supported formats: .json (JSONParser), .yaml/.yml (YAMLParser), .pine (PineParser).
Files that fail parsing, validation, or backtest are recorded as rejected.
Files already registered with the same mtime are recorded as unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pandas as pd

from src.infrastructure.logger import get_logger

from .parsers.json_parser import JSONParser
from .parsers.pine_parser import PineParser
from .parsers.yaml_parser import YAMLParser
from .registry import RegistrationError, StrategyRegistry
from .uss.schema import USSSpec

logger = get_logger(__name__)

_SUPPORTED_EXTS = frozenset({".json", ".yaml", ".yml", ".pine"})


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class FileOutcome:
    path: Path
    strategy_name: str
    status: Literal["accepted", "rejected", "unchanged"]
    reason: str = ""


@dataclass
class DiscoveryScanResult:
    accepted: list[FileOutcome] = field(default_factory=list)
    rejected: list[FileOutcome] = field(default_factory=list)
    unchanged: list[FileOutcome] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"accepted={len(self.accepted)} "
            f"rejected={len(self.rejected)} "
            f"unchanged={len(self.unchanged)}"
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class StrategyDiscoveryEngine:
    """
    Scans configured directories for strategy files and registers them.

    Tracks file mtimes so repeated scans skip unchanged files without re-parsing.
    """

    def __init__(
        self,
        registry: StrategyRegistry,
        dirs: list[Path] | None = None,
    ) -> None:
        self._registry = registry
        self._dirs: list[Path] = dirs if dirs is not None else []
        self._mtime_cache: dict[Path, float] = {}

    def scan(
        self,
        df: pd.DataFrame | None = None,
        skip_backtest: bool = False,
    ) -> DiscoveryScanResult:
        """Scan all configured directories. Returns per-file outcomes."""
        import json as _json

        # Load enabled/disabled flags — same source of truth as registry.reload()
        _reg_path = Path(__file__).parent.parent.parent / "config" / "strategies_registry.json"
        _registry_enabled: dict[str, bool] = {}
        if _reg_path.exists():
            try:
                for entry in _json.loads(_reg_path.read_text(encoding="utf-8")).get(
                    "strategies", []
                ):
                    _registry_enabled[entry["name"]] = bool(entry.get("enabled", True))
            except Exception:
                pass

        result = DiscoveryScanResult()
        for directory in self._dirs:
            if not directory.exists():
                continue
            for ext in sorted(_SUPPORTED_EXTS):
                for path in sorted(directory.glob(f"*{ext}")):
                    name = path.stem
                    if not _registry_enabled.get(name, True):
                        result.rejected.append(
                            FileOutcome(
                                path=path,
                                strategy_name=name,
                                status="rejected",
                                reason="disabled in strategies_registry.json",
                            )
                        )
                        continue
                    outcome = self._process_file(path, df, skip_backtest)
                    if outcome.status == "accepted":
                        result.accepted.append(outcome)
                    elif outcome.status == "rejected":
                        result.rejected.append(outcome)
                    else:
                        result.unchanged.append(outcome)
        logger.info("scan complete: %s", result.summary())
        return result

    def _process_file(
        self,
        path: Path,
        df: pd.DataFrame | None,
        skip_backtest: bool,
    ) -> FileOutcome:
        mtime = path.stat().st_mtime
        if self._mtime_cache.get(path) == mtime and self._registry.get(path.stem) is not None:
            return FileOutcome(
                path=path,
                strategy_name=path.stem,
                status="unchanged",
                reason="mtime unchanged",
            )

        try:
            spec = self._parse_file(path)
        except Exception as exc:
            return FileOutcome(
                path=path,
                strategy_name=path.stem,
                status="rejected",
                reason=f"Parse error: {exc}",
            )

        try:
            self._registry.register_spec(
                spec,
                df=df,
                skip_backtest=skip_backtest or df is None,
            )
        except RegistrationError as exc:
            return FileOutcome(
                path=path,
                strategy_name=spec.meta.name,
                status="rejected",
                reason=str(exc),
            )

        self._mtime_cache[path] = mtime
        logger.info("discovered strategy=%s from %s", spec.meta.name, path.name)
        return FileOutcome(
            path=path,
            strategy_name=spec.meta.name,
            status="accepted",
        )

    @staticmethod
    def _parse_file(path: Path) -> USSSpec:
        source = path.read_text(encoding="utf-8")
        ext = path.suffix.lower()
        if ext == ".json":
            return JSONParser().parse(source)
        if ext in (".yaml", ".yml"):
            return YAMLParser().parse(source)
        if ext == ".pine":
            return PineParser().parse(source)
        raise ValueError(f"Unsupported format: {ext}")
