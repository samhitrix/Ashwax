from types import SimpleNamespace

from src.orchestrator import _held_symbols_from_holdings


def _h(sym: str, qty: int = 0, t1: int = 0) -> SimpleNamespace:
    return SimpleNamespace(symbol=sym, quantity=qty, t1_quantity=t1)


def test_settled_holding_counts_as_held() -> None:
    assert _held_symbols_from_holdings([_h("LAURUSLABS", 26)]) == {"LAURUSLABS"}


def test_t1_pending_holding_counts_as_held() -> None:
    # Bought the previous session, not yet moved into `quantity` (CGCL, 2026-09-08).
    assert _held_symbols_from_holdings([_h("CGCL", 0, 45)]) == {"CGCL"}


def test_zero_quantity_and_exchange_prefix_are_handled() -> None:
    got = _held_symbols_from_holdings([_h("SYMBIOTEC", 0, 0), _h("NSE:INFY", 5)])
    assert got == {"INFY"}


def test_none_or_empty_is_empty_set() -> None:
    assert _held_symbols_from_holdings(None) == set()
    assert _held_symbols_from_holdings([]) == set()
