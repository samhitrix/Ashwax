"""
strategies/compiler/ — re-exports from strategies/uss/ for spec path alignment.

docs/13-file-structure.md specifies src/strategies/compiler/ but the
implementation lives in src/strategies/uss/. This bridges both import paths.
"""

from src.strategies.uss.compiler import USSCompiler
from src.strategies.uss.schema import USSSpec
from src.strategies.uss.validator import USSValidator

__all__ = ["USSSpec", "USSValidator", "USSCompiler"]
