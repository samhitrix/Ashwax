"""USSParser — parses direct USS JSON (Format 5: LLM-Generated)."""

from __future__ import annotations

import json

from src.strategies.uss.schema import USSSpec
from src.strategies.uss.validator import USSValidator


class USSParser:
    def __init__(self) -> None:
        self._validator = USSValidator()

    def parse(self, uss_input: str | dict[str, object]) -> USSSpec:
        data: dict[str, object] = (
            json.loads(uss_input) if isinstance(uss_input, str) else dict(uss_input)
        )
        spec: USSSpec = (
            USSSpec.from_dict(data) if hasattr(USSSpec, "from_dict") else USSSpec(**data)
        )
        result = self._validator.validate(spec)
        if not result.ok:
            raise ValueError(f"USS validation failed: {result.errors}")
        return spec
