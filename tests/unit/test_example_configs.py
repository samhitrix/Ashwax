"""config/examples must stay paper-only and structurally identical to the working config."""

from __future__ import annotations

import json
from pathlib import Path

_CFG = Path(__file__).resolve().parents[2] / "config"
_EX = _CFG / "examples"
_FILES = ("global.json", "operator.json", "strategies_registry.json")


def _load(p: Path):  # type: ignore[no-untyped-def]
    return json.loads(p.read_text(encoding="utf-8"))


def _walk(node, path=""):  # type: ignore[no-untyped-def]
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, node


def test_examples_never_enable_real_money() -> None:
    for name in _FILES:
        for path, value in _walk(_load(_EX / name)):
            assert not (path.endswith(".paper_mode") and value is False), f"{name}:{path}"
            assert not (path.endswith(".execution_mode") and value == "live"), f"{name}:{path}"
    assert _load(_EX / "operator.json")["account"]["paper_mode_locked"] is True


def _strip_safety(node):  # type: ignore[no-untyped-def]
    """Drop the exact keys the examples are allowed to differ on."""
    if isinstance(node, dict):
        return {
            k: _strip_safety(v)
            for k, v in node.items()
            if k not in ("paper_mode", "paper_mode_locked", "execution_mode")
        }
    if isinstance(node, list):
        return [_strip_safety(v) for v in node]
    return node


def test_examples_differ_from_working_config_only_in_safety_keys() -> None:
    for name in _FILES:
        assert _strip_safety(_load(_EX / name)) == _strip_safety(_load(_CFG / name)), name
