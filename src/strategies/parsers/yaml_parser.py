"""
YAML DSL parser — converts YAML strategy spec to USSSpec.

Structurally identical to the JSON DSL; loads YAML to dict then delegates
to JSONParser. Sets source_format="yaml" on the resulting spec.
"""

from __future__ import annotations

from typing import Any

import yaml

from ..uss.schema import USSSpec
from .json_parser import JSONParseError, JSONParser


class YAMLParseError(ValueError):
    pass


class YAMLParser:
    """Parses YAML DSL string or dict → USSSpec."""

    def __init__(self) -> None:
        self._json_parser = JSONParser()

    def parse(self, source: str | dict[str, Any]) -> USSSpec:
        if isinstance(source, str):
            try:
                data: Any = yaml.safe_load(source)
            except yaml.YAMLError as exc:
                raise YAMLParseError(f"Invalid YAML: {exc}") from exc
            if not isinstance(data, dict):
                raise YAMLParseError("YAML root must be a mapping")
        else:
            data = source

        try:
            spec = self._json_parser.parse(data)
        except JSONParseError as exc:
            raise YAMLParseError(str(exc)) from exc

        return spec.model_copy(update={"source_format": "yaml"})
