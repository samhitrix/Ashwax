"""
Broker registry — maps broker names to adapter classes.

Extend this dict when adding a new broker adapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.brokers.base import BrokerInterface

# Registry: name → (module_path, class_name)
_REGISTRY: dict[str, tuple[str, str]] = {
    "kite": ("src.brokers.adapters.kite.adapter", "KiteAdapter"),
    "angel_one": ("src.brokers.adapters.angel_one.adapter", "AngelOneAdapter"),
    "paper": ("src.brokers.adapters.paper.adapter", "PaperAdapter"),
}

SUPPORTED_BROKERS: frozenset[str] = frozenset(_REGISTRY.keys())


def list_brokers() -> list[str]:
    """Return sorted list of registered broker names."""
    return sorted(_REGISTRY.keys())


def get_adapter_class(name: str) -> type[BrokerInterface]:
    """
    Return the adapter class for the given broker name.

    Raises ValueError for unknown broker names.
    Lazy-imports to avoid loading all broker SDKs at startup.
    """
    if name not in _REGISTRY:
        raise ValueError(f"Unknown broker {name!r}. Supported: {sorted(_REGISTRY)}")

    import importlib

    module_path, class_name = _REGISTRY[name]
    module = importlib.import_module(module_path)
    return getattr(module, class_name)  # type: ignore[no-any-return]
