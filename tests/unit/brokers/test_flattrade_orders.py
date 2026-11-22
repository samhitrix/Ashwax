"""Flattrade orders: PlaceOrder / ModifyOrder / CancelOrder / SingleOrdHist / OrderBook /
TradeBook / GetBasketMargin (findings A1, A3, A4, A7, C-4, C-6, C-9, C-11).

Every HTTP call goes through FlattradeClient with an injected fake transport.
No network, no credentials, no live order.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import pytest
from src.brokers.adapters.flattrade import adapter as adapter_mod
from src.brokers.adapters.flattrade.client import FlattradeError, FlattradeTransportError
from src.brokers.base import GTTRequest, OrderRequest, OrderWindowClosed

from tests.unit.brokers.fixtures.flattrade_fakes import (
    NOW,
    Router,
    make_adapter,
    order_row,
    quote,
    seq,
)

_UID = r"[0-9a-f]{8}"


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_mod, "_now", lambda: NOW)
    monkeypatch.setattr(adapter_mod, "_order_variety", lambda now: "REGULAR")


@pytest.fixture(autouse=True)
def _live_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Order tests run with live_orders_verified forced True (the shipped value is
    False; test_live_orders_are_off_by_default checks the gate)."""
    monkeypatch.setitem(adapter_mod._CAPS, "live_orders_verified", True)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def _sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", _sleep)
    return slept


def _mine(router: Router, **fields: Any) -> dict[str, Any]:
    """An OrderBook row carrying the remarks of the last PlaceOrder attempt."""
    return order_row(remarks=router.payloads("PlaceOrder")[-1]["remarks"], **fields)


def _req(**kw: Any) -> OrderRequest:
    base: dict[str, Any] = {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "direction": "BUY",
        "quantity": 10,
        "order_type": "LIMIT",
        "product": "INTRADAY",
        "validity": "DAY",
        "agent_trade_id": "T1",
        "signal_id": "S1",
        "price": 2500.0,
    }
    base.update(kw)
    return OrderRequest(**base)


def _ok_place(p: dict[str, Any]) -> dict[str, Any]:
    return {"stat": "Ok", "norenordno": "26101500000123", "request_time": "x"}


def _ambiguous(endpoint: str) -> FlattradeTransportError:
    return FlattradeTransportError(endpoint, ambiguous=True, cause=TimeoutError())


# ── order-type conversion table ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("direction", "price", "ti", "expected"),
    [
        ("BUY", 2500.03, "0.05", "2500.00"),  # never pay more than asked: floor
        ("SELL", 2500.03, "0.05", "2500.05"),  # never sell lower than asked: ceil
        ("BUY", 2500.07, "0.10", "2500.00"),
        ("SELL", 2500.00, "0.05", "2500.00"),
    ],
)
async def test_limit_is_lmt_rounded_to_tick_without_trgprc(
    direction: str, price: float, ti: str, expected: str
) -> None:
    adapter, router, limiter = make_adapter(
        {"GetQuotes": quote("2500", ti), "PlaceOrder": _ok_place}
    )
    oid = await adapter.place_order(_req(direction=direction, price=price))
    assert oid == "26101500000123"
    (p,) = router.payloads("PlaceOrder")
    assert p["prctyp"] == "LMT"
    assert p["prc"] == expected
    assert "trgprc" not in p
    assert p["trantype"] == direction[0]
    assert "order" in limiter.categories


async def test_place_payload_fields_are_strings_and_mapped() -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("2500"), "PlaceOrder": _ok_place})
    await adapter.place_order(_req(quantity=7, product="DELIVERY", validity="IOC", tag="tagX"))
    (p,) = router.payloads("PlaceOrder")
    assert p["exch"] == "NSE"
    assert p["tsym"] == "RELIANCE-EQ"
    assert p["qty"] == "7"
    assert p["prd"] == "C"
    assert p["ret"] == "IOC"
    assert p["ordersource"] == "API"
    assert re.fullmatch(rf"tagX-{_UID}", p["remarks"])
    assert p["uid"] == "FZ00000"
    for k in ("qty", "prc"):
        assert isinstance(p[k], str)
    assert "amo" not in p


@pytest.mark.parametrize(
    ("product", "prd"),
    [
        ("INTRADAY", "I"),
        ("DELIVERY", "C"),
        ("MARGIN", "M"),
        ("MIS", "I"),  # Kite-style aliases (review I3)
        ("CNC", "C"),
        ("NRML", "M"),
        ("mis", "I"),
    ],
)
async def test_product_map(product: str, prd: str) -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("2500"), "PlaceOrder": _ok_place})
    await adapter.place_order(_req(product=product))
    assert router.payloads("PlaceOrder")[0]["prd"] == prd


async def test_remarks_is_tag_prefix_plus_unique_id_within_20() -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("2500"), "PlaceOrder": _ok_place})
    await adapter.place_order(_req(agent_trade_id="A" * 30))
    await adapter.place_order(_req(agent_trade_id="A" * 30))
    first, second = (p["remarks"] for p in router.payloads("PlaceOrder"))
    assert re.fullmatch(rf"A{{11}}-{_UID}", first)
    assert len(first) == 20
    assert first != second  # unique per attempt, even with a constant tag


@pytest.mark.parametrize(
    ("direction", "trigger", "price", "trg", "prc"),
    [("BUY", 2500.0, 2505.0, "2500.00", "2505.00"), ("SELL", 2500.0, 2495.0, "2500.00", "2495.00")],
)
async def test_sl_is_sl_lmt_with_trigger_and_price(
    direction: str, trigger: float, price: float, trg: str, prc: str
) -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("2490"), "PlaceOrder": _ok_place})
    await adapter.place_order(
        _req(direction=direction, order_type="SL", price=price, trigger_price=trigger)
    )
    (p,) = router.payloads("PlaceOrder")
    assert (p["prctyp"], p["trgprc"], p["prc"]) == ("SL-LMT", trg, prc)
    assert isinstance(p["trgprc"], str)


@pytest.mark.parametrize(
    ("direction", "price", "trigger"), [("BUY", 2495.0, 2500.0), ("SELL", 2505.0, 2500.0)]
)
async def test_sl_limit_on_wrong_side_of_trigger_raises_before_any_order(
    direction: str, price: float, trigger: float
) -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("2490"), "PlaceOrder": _ok_place})
    with pytest.raises(ValueError, match="trigger"):
        await adapter.place_order(
            _req(direction=direction, order_type="SL", price=price, trigger_price=trigger)
        )
    assert router.payloads("PlaceOrder") == []


@pytest.mark.parametrize(
    ("direction", "ti", "expected"),
    [
        ("BUY", "0.05", "2513.85"),  # 2501.30 * 1.005 = 2513.8065 -> UP
        ("SELL", "0.05", "2488.75"),  # 2501.30 * 0.995 = 2488.7935 -> DOWN
        ("BUY", "0.10", "2513.90"),
        ("SELL", "0.10", "2488.70"),
    ],
)
async def test_market_becomes_marketable_lmt_with_band(
    direction: str, ti: str, expected: str
) -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("2501.30", ti), "PlaceOrder": _ok_place})
    await adapter.place_order(_req(direction=direction, order_type="MARKET", price=None))
    (p,) = router.payloads("PlaceOrder")
    assert p["prctyp"] == "LMT"
    assert p["prc"] == expected
    assert "trgprc" not in p


@pytest.mark.parametrize(
    ("direction", "fields", "expected"),
    [
        ("BUY", {"sp1": "2502.00", "bp1": "2501.00"}, "2514.55"),  # ask*1.005 UP
        ("SELL", {"sp1": "2502.00", "bp1": "2500.00"}, "2487.50"),  # bid*0.995 DOWN
        ("BUY", {"sp1": "0"}, "2513.85"),  # no ask -> LTP
        ("SELL", {"bp1": ""}, "2488.75"),  # no bid -> LTP
        ("BUY", {"sp1": "2499.00"}, "2513.85"),  # stale ask below LTP -> LTP
    ],
)
async def test_market_is_priced_off_the_opposite_side(
    direction: str, fields: dict[str, str], expected: str
) -> None:
    adapter, router, _ = make_adapter(
        {"GetQuotes": quote("2501.30", **fields), "PlaceOrder": _ok_place}
    )
    await adapter.place_order(_req(direction=direction, order_type="MARKET", price=None))
    assert router.payloads("PlaceOrder")[0]["prc"] == expected


async def test_market_is_sent_ioc_in_session_and_amo_keeps_validity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("2500"), "PlaceOrder": _ok_place})
    await adapter.place_order(_req(order_type="MARKET", price=None, validity="DAY"))
    await adapter.place_order(_req(validity="DAY"))  # LIMIT keeps DAY
    monkeypatch.setattr(adapter_mod, "_order_variety", lambda now: "AMO")
    await adapter.place_order(_req(order_type="MARKET", price=None, validity="DAY"))
    market, limit, amo = router.payloads("PlaceOrder")
    assert (market["prctyp"], market["ret"]) == ("LMT", "IOC")
    assert limit["ret"] == "DAY"
    assert amo["ret"] == "DAY"


async def test_market_band_comes_from_adapter_config() -> None:
    adapter, router, _ = make_adapter(
        {"GetQuotes": quote("1000.00"), "PlaceOrder": _ok_place}, band=0.01
    )
    await adapter.place_order(_req(order_type="MARKET", price=None))
    assert router.payloads("PlaceOrder")[0]["prc"] == "1010.00"


async def test_market_price_is_clamped_to_circuit_limits() -> None:
    adapter, router, _ = make_adapter(
        {"GetQuotes": quote("2501.30", uc="2510.00", lc="2490.00"), "PlaceOrder": _ok_place}
    )
    await adapter.place_order(_req(order_type="MARKET", price=None))
    await adapter.place_order(_req(direction="SELL", order_type="MARKET", price=None))
    buy, sell = router.payloads("PlaceOrder")
    assert buy["prc"] == "2510.00"
    assert sell["prc"] == "2490.00"


@pytest.mark.parametrize("lp", ["0", "", None])
async def test_market_without_ltp_fails_loud(lp: Any) -> None:
    q = quote("1")
    q["lp"] = lp
    adapter, router, _ = make_adapter({"GetQuotes": q, "PlaceOrder": _ok_place})
    with pytest.raises(FlattradeError, match="LTP"):
        await adapter.place_order(_req(order_type="MARKET", price=None))
    assert router.payloads("PlaceOrder") == []


@pytest.mark.parametrize(
    ("direction", "trigger", "trg", "prc"),
    [
        ("BUY", 2500.0, "2500.00", "2512.50"),
        ("SELL", 2500.0, "2500.00", "2487.50"),
        ("BUY", 2500.03, "2500.05", "2512.60"),  # 2500.05 * 1.005 = 2512.55025 -> UP
        ("SELL", 2500.03, "2500.05", "2487.50"),  # 2500.05 * 0.995 = 2487.54975 -> DOWN
    ],
)
async def test_sl_m_becomes_sl_lmt_with_band_beyond_trigger(
    direction: str, trigger: float, trg: str, prc: str
) -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("2490"), "PlaceOrder": _ok_place})
    await adapter.place_order(
        _req(direction=direction, order_type="SL-M", price=None, trigger_price=trigger)
    )
    (p,) = router.payloads("PlaceOrder")
    assert (p["prctyp"], p["trgprc"], p["prc"]) == ("SL-LMT", trg, prc)


@pytest.mark.parametrize(
    "kw",
    [
        {"order_type": "LIMIT", "price": None},
        {"order_type": "LIMIT", "price": 0.0},
        {"order_type": "SL", "price": 100.0, "trigger_price": None},
        {"order_type": "SL-M", "trigger_price": None},
        {"order_type": "BRACKET"},
        {"direction": "LONG"},
        {"product": "CO"},
        {"validity": "GTC"},
        {"quantity": 0},
    ],
)
async def test_invalid_requests_raise_before_any_order(kw: dict[str, Any]) -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("2500"), "PlaceOrder": _ok_place})
    with pytest.raises(ValueError):
        await adapter.place_order(_req(**kw))
    assert router.payloads("PlaceOrder") == []


async def test_limit_rounding_to_zero_fails_loud() -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("0.05"), "PlaceOrder": _ok_place})
    with pytest.raises(ValueError, match="zero"):
        await adapter.place_order(_req(price=0.03))
    assert router.payloads("PlaceOrder") == []


async def test_sell_market_at_one_tick_ltp_stays_one_tick() -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("0.05"), "PlaceOrder": _ok_place})
    await adapter.place_order(_req(direction="SELL", order_type="MARKET", price=None))
    assert router.payloads("PlaceOrder")[0]["prc"] == "0.05"


async def test_unknown_symbol_raises_key_error() -> None:
    adapter, router, _ = make_adapter({"PlaceOrder": _ok_place})
    with pytest.raises(KeyError):
        await adapter.place_order(_req(symbol="NOSUCH"))
    assert router.payloads("PlaceOrder") == []


async def test_tick_is_cached_per_symbol() -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("2500", "0.10"), "PlaceOrder": _ok_place})
    await adapter.place_order(_req())
    await adapter.place_order(_req())
    assert len(router.payloads("GetQuotes")) == 1


async def test_nfo_option_uses_scrip_master_tradingsymbol() -> None:
    adapter, router, _ = make_adapter(
        {"GetQuotes": quote("101.00", exch="NFO", token="62354"), "PlaceOrder": _ok_place}
    )
    await adapter.place_order(
        _req(
            symbol="NIFTY29DEC26P21050", exchange="NFO", product="MARGIN", quantity=65, price=101.0
        )
    )
    p = router.payloads("PlaceOrder")[0]
    assert (p["exch"], p["tsym"], p["prd"], p["qty"]) == ("NFO", "NIFTY29DEC26P21050", "M", "65")


# ── AMO / order window ────────────────────────────────────────────────────────


async def test_amo_key_only_for_amo_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("2500"), "PlaceOrder": _ok_place})
    await adapter.place_order(_req())
    monkeypatch.setattr(adapter_mod, "_order_variety", lambda now: "AMO")
    await adapter.place_order(_req())
    regular, amo = router.payloads("PlaceOrder")
    assert "amo" not in regular
    assert amo["amo"] == "Yes"


async def test_closed_window_raises_and_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_mod, "_order_variety", lambda now: "CLOSED")
    adapter, router, _ = make_adapter({"GetQuotes": quote("2500"), "PlaceOrder": _ok_place})
    with pytest.raises(OrderWindowClosed):
        await adapter.place_order(_req())
    assert router.calls == []


# ── UserDetails enablement gate ───────────────────────────────────────────────


async def test_user_details_called_once_per_session() -> None:
    adapter, router, _ = make_adapter({"GetQuotes": quote("2500"), "PlaceOrder": _ok_place})
    await adapter.place_order(_req())
    await adapter.place_order(_req())
    assert len(router.payloads("UserDetails")) == 1
    adapter.set_access_token("tok2")  # a new login must re-check enablement
    await adapter.place_order(_req())
    assert len(router.payloads("UserDetails")) == 2


@pytest.mark.parametrize(
    ("details", "kw", "match"),
    [
        ({"orarr": ["SL-LMT"]}, {}, "LMT"),
        ({"orarr": ["LMT"]}, {"order_type": "SL", "trigger_price": 2499.0}, "SL-LMT"),
        (
            {"orarr": ["LMT"]},
            {"order_type": "SL-M", "trigger_price": 2499.0, "price": None},
            "SL-LMT",
        ),
        ({"prarr": [{"prd": "C", "exch": ["NSE"]}]}, {}, "product"),
        ({"prarr": [{"prd": "I", "exch": ["NFO"]}]}, {}, "NSE"),
        ({"exarr": ["NFO"]}, {}, "NSE"),
        ({"orarr": None}, {}, "orarr"),
        ({"prarr": None}, {}, "prarr"),
        ({"orarr": [{"x": 1}]}, {}, "orarr"),
        ({"prarr": [{"exch": ["NSE"]}]}, {}, "prarr"),
    ],
)
async def test_enablement_failure_raises_before_placing(
    details: dict[str, Any], kw: dict[str, Any], match: str
) -> None:
    from tests.unit.brokers.fixtures.flattrade_fakes import USER_DETAILS

    ud = {**USER_DETAILS, **details}
    ud = {k: v for k, v in ud.items() if v is not None}
    adapter, router, _ = make_adapter(
        {"UserDetails": ud, "GetQuotes": quote("2500"), "PlaceOrder": _ok_place}
    )
    if "trigger_price" in kw and kw.get("order_type") == "SL":
        kw = {**kw, "price": 2500.0}
    with pytest.raises(FlattradeError, match=match):
        await adapter.place_order(_req(**kw))
    assert router.payloads("PlaceOrder") == []


async def test_failed_user_details_is_not_cached() -> None:
    adapter, router, _ = make_adapter(
        {
            "UserDetails": seq(FlattradeError("UserDetails: Server Timeout"), _ud()),
            "GetQuotes": quote("2500"),
            "PlaceOrder": _ok_place,
        }
    )
    with pytest.raises(FlattradeError):
        await adapter.place_order(_req())
    assert await adapter.place_order(_req()) == "26101500000123"


def _ud() -> dict[str, Any]:
    from tests.unit.brokers.fixtures.flattrade_fakes import USER_DETAILS

    return dict(USER_DETAILS)


# ── broker replies ────────────────────────────────────────────────────────────


async def test_rejection_raises_flattrade_error() -> None:
    adapter, _, _ = make_adapter(
        {"GetQuotes": quote("2500"), "PlaceOrder": {"stat": "Not_Ok", "emsg": "Invalid Input"}}
    )
    with pytest.raises(FlattradeError, match="Invalid Input"):
        await adapter.place_order(_req())


async def test_ok_reply_without_order_number_raises() -> None:
    adapter, _, _ = make_adapter({"GetQuotes": quote("2500"), "PlaceOrder": {"stat": "Ok"}})
    with pytest.raises(FlattradeError, match="norenordno"):
        await adapter.place_order(_req())


# ── ambiguous transport: never place twice ────────────────────────────────────


async def test_ambiguous_place_found_in_order_book_returns_it_without_replacing() -> None:
    adapter, router, _ = make_adapter(
        {"GetQuotes": quote("2500"), "PlaceOrder": _ambiguous("PlaceOrder")}
    )
    router.replies["OrderBook"] = lambda p: [
        order_row(norenordno="OTHER1", remarks="T9"),
        _mine(router, norenordno="26101500000999"),
    ]
    oid = await adapter.place_order(_req())
    assert oid == "26101500000999"
    assert len(router.payloads("PlaceOrder")) == 1
    assert len(router.payloads("OrderBook")) == 1


async def test_ambiguous_place_constant_tag_older_order_is_not_matched() -> None:
    """Review I1: an earlier order with the same tag prefix (another attempt) and a
    fresh entry time must not be taken for this attempt."""
    adapter, router, _ = make_adapter(
        {
            "GetQuotes": quote("2500"),
            "PlaceOrder": _ambiguous("PlaceOrder"),
            "OrderBook": [order_row(norenordno="OLD", remarks="T1-0badc0de")],
        }
    )
    with pytest.raises(FlattradeTransportError):
        await adapter.place_order(_req())


async def test_ambiguous_place_repolls_until_the_order_is_listed(_no_sleep: list[float]) -> None:
    adapter, router, _ = make_adapter(
        {"GetQuotes": quote("2500"), "PlaceOrder": _ambiguous("PlaceOrder")}
    )
    reads: list[int] = []

    def book(p: dict[str, Any]) -> list[dict[str, Any]]:
        reads.append(1)
        return [] if len(reads) < 2 else [_mine(router, norenordno="LATE1")]

    router.replies["OrderBook"] = book
    assert await adapter.place_order(_req()) == "LATE1"
    assert len(reads) == 2
    assert _no_sleep == [adapter_mod._AMBIGUOUS_POLL_DELAY_S]
    assert len(router.payloads("PlaceOrder")) == 1


async def test_ambiguous_place_row_without_parseable_time_is_not_accepted() -> None:
    adapter, router, _ = make_adapter(
        {"GetQuotes": quote("2500"), "PlaceOrder": _ambiguous("PlaceOrder")}
    )
    router.replies["OrderBook"] = lambda p: [_mine(router, ordenttm="garbage", norentm="")]
    with pytest.raises(FlattradeTransportError):
        await adapter.place_order(_req())


async def test_ambiguous_place_not_found_raises_and_never_replaces() -> None:
    adapter, router, _ = make_adapter(
        {
            "GetQuotes": quote("2500"),
            "PlaceOrder": _ambiguous("PlaceOrder"),
            "OrderBook": {"stat": "Not_Ok", "emsg": "No Data :  "},
        }
    )
    with pytest.raises(FlattradeTransportError) as ei:
        await adapter.place_order(_req())
    assert ei.value.ambiguous is True
    assert len(router.payloads("PlaceOrder")) == 1
    assert len(router.payloads("OrderBook")) == adapter_mod._AMBIGUOUS_POLLS


@pytest.mark.parametrize(
    "fields",
    [
        {"qty": "11"},
        {"trantype": "S"},
        {"tsym": "SBIN-EQ"},
        {"prctyp": "SL-LMT"},
        {"ordenttm": str(int((NOW - timedelta(hours=1)).timestamp()))},  # stale
        {"ordenttm": str(int((NOW + timedelta(hours=1)).timestamp()))},  # beyond window
    ],
)
async def test_ambiguous_place_ignores_non_matching_rows(fields: dict[str, Any]) -> None:
    adapter, router, _ = make_adapter(
        {"GetQuotes": quote("2500"), "PlaceOrder": _ambiguous("PlaceOrder")}
    )
    router.replies["OrderBook"] = lambda p: [_mine(router, **fields)]
    with pytest.raises(FlattradeTransportError):
        await adapter.place_order(_req())
    assert len(router.payloads("PlaceOrder")) == 1


async def test_ambiguous_place_excludes_orders_this_adapter_already_placed() -> None:
    adapter, router, _ = make_adapter(
        {"GetQuotes": quote("2500"), "PlaceOrder": seq(_ok_place({}), _ambiguous("PlaceOrder"))}
    )
    # the book lists only the FIRST (already returned) order id, even if it matched
    router.replies["OrderBook"] = lambda p: [_mine(router)]
    assert await adapter.place_order(_req()) == "26101500000123"
    with pytest.raises(FlattradeTransportError):
        await adapter.place_order(_req())


async def test_ambiguous_place_with_two_candidates_raises_without_guessing() -> None:
    adapter, router, _ = make_adapter(
        {"GetQuotes": quote("2500"), "PlaceOrder": _ambiguous("PlaceOrder")}
    )
    router.replies["OrderBook"] = lambda p: [
        _mine(router, norenordno="A1"),
        _mine(router, norenordno="A2"),
    ]
    with pytest.raises(FlattradeError, match="A1.*A2|A2.*A1"):
        await adapter.place_order(_req())
    assert len(router.payloads("PlaceOrder")) == 1


async def test_ambiguous_place_lookup_failure_raises_original() -> None:
    adapter, router, _ = make_adapter(
        {
            "GetQuotes": quote("2500"),
            "PlaceOrder": _ambiguous("PlaceOrder"),
            "OrderBook": FlattradeTransportError("OrderBook", ambiguous=False, cause=OSError()),
        }
    )
    with pytest.raises(FlattradeTransportError) as ei:
        await adapter.place_order(_req())
    assert ei.value.endpoint == "PlaceOrder"
    assert len(router.payloads("PlaceOrder")) == 1


async def test_ambiguous_untagged_place_matches_only_its_unique_id() -> None:
    adapter, router, _ = make_adapter(
        {"GetQuotes": quote("2500"), "PlaceOrder": _ambiguous("PlaceOrder")}
    )
    router.replies["OrderBook"] = lambda p: [
        order_row(norenordno="MANUAL", remarks=""),  # a manual order on the same scrip
        _mine(router, norenordno="OURS"),
    ]
    assert await adapter.place_order(_req(agent_trade_id="", tag="")) == "OURS"
    assert re.fullmatch(_UID, router.payloads("PlaceOrder")[0]["remarks"])
    assert len(router.payloads("PlaceOrder")) == 1


async def test_unambiguous_transport_failure_skips_lookup() -> None:
    adapter, router, _ = make_adapter(
        {
            "GetQuotes": quote("2500"),
            "PlaceOrder": FlattradeTransportError("PlaceOrder", ambiguous=False, cause=OSError()),
        }
    )
    with pytest.raises(FlattradeTransportError):
        await adapter.place_order(_req())
    assert router.payloads("OrderBook") == []


# ── modify / cancel ───────────────────────────────────────────────────────────


async def test_modify_sends_total_qty_full_field_set_and_no_trgprc_for_lmt() -> None:
    hist = [
        order_row(status="PENDING", rpt="NewAck"),
        order_row(qty="100", fillshares="40", status="OPEN", rpt="Replaced"),
    ]
    adapter, router, limiter = make_adapter(
        {"SingleOrdHist": hist, "ModifyOrder": {"stat": "Ok", "result": "26101500000123"}}
    )
    assert await adapter.modify_order("26101500000123", {"price": 2490.02}) == "26101500000123"
    (p,) = router.payloads("ModifyOrder")
    assert p["norenordno"] == "26101500000123"
    assert (p["exch"], p["tsym"], p["prctyp"], p["ret"]) == ("NSE", "RELIANCE-EQ", "LMT", "DAY")
    assert p["qty"] == "100"  # TOTAL, not pending (60)
    assert p["prc"] == "2490.00"  # BUY limit floors
    assert "trgprc" not in p
    assert "order" in limiter.categories


async def test_modify_sl_trigger_only_shifts_limit_by_same_delta() -> None:
    hist = [order_row(prctyp="SL-LMT", trantype="S", trgprc="2500.00", prc="2487.50")]
    adapter, router, _ = make_adapter(
        {"SingleOrdHist": hist, "ModifyOrder": {"stat": "Ok", "result": "X"}}
    )
    await adapter.modify_order("26101500000123", {"trigger_price": 2510.0})
    (p,) = router.payloads("ModifyOrder")
    assert (p["prctyp"], p["trgprc"], p["prc"]) == ("SL-LMT", "2510.00", "2497.50")


async def test_modify_quantity_and_explicit_sl_price() -> None:
    hist = [order_row(prctyp="SL-LMT", trantype="B", trgprc="2500.00", prc="2505.00")]
    adapter, router, _ = make_adapter(
        {"SingleOrdHist": hist, "ModifyOrder": {"stat": "Ok", "result": "X"}}
    )
    await adapter.modify_order(
        "26101500000123", {"quantity": 20, "price": 2506.0, "trigger_price": 2501.0}
    )
    (p,) = router.payloads("ModifyOrder")
    assert (p["qty"], p["trgprc"], p["prc"]) == ("20", "2501.00", "2506.00")


@pytest.mark.parametrize(
    ("params", "exc"),
    [
        ({"tag": "x"}, ValueError),
        ({"order_type": "MARKET"}, ValueError),
        ({"trigger_price": 2400.0}, ValueError),  # trigger on a LIMIT order
        ({"quantity": 0}, ValueError),
    ],
)
async def test_modify_rejects_bad_params_before_sending(params: dict[str, Any], exc: type) -> None:
    adapter, router, _ = make_adapter(
        {"SingleOrdHist": [order_row()], "ModifyOrder": {"stat": "Ok"}}
    )
    with pytest.raises(exc):
        await adapter.modify_order("26101500000123", params)
    assert router.payloads("ModifyOrder") == []


@pytest.mark.parametrize("status", ["COMPLETE", "REJECTED", "CANCELED"])
async def test_modify_terminal_order_raises_without_sending(status: str) -> None:
    adapter, router, _ = make_adapter(
        {"SingleOrdHist": [order_row(status=status)], "ModifyOrder": {"stat": "Ok"}}
    )
    with pytest.raises(FlattradeError, match="terminal"):
        await adapter.modify_order("26101500000123", {"price": 2490.0})
    assert router.payloads("ModifyOrder") == []


async def test_modify_ambiguous_reports_true_state() -> None:
    adapter, router, _ = make_adapter(
        {
            "SingleOrdHist": seq([order_row()], [order_row(prc="2490.00")]),
            "ModifyOrder": _ambiguous("ModifyOrder"),
        }
    )
    assert await adapter.modify_order("26101500000123", {"price": 2490.0}) == "26101500000123"
    assert len(router.payloads("ModifyOrder")) == 1


async def test_modify_ambiguous_not_applied_raises() -> None:
    adapter, router, _ = make_adapter(
        {
            "SingleOrdHist": seq([order_row()], [order_row()]),
            "ModifyOrder": _ambiguous("ModifyOrder"),
        }
    )
    with pytest.raises(FlattradeError, match="prc=2500"):
        await adapter.modify_order("26101500000123", {"price": 2490.0})
    assert len(router.payloads("ModifyOrder")) == 1


async def test_cancel_sends_order_number_and_returns_true() -> None:
    adapter, router, limiter = make_adapter({"CancelOrder": {"stat": "Ok", "result": "1"}})
    assert await adapter.cancel_order("26101500000123") is True
    assert router.payloads("CancelOrder")[0]["norenordno"] == "26101500000123"
    assert "order" in limiter.categories


async def test_cancel_ambiguous_confirmed_cancelled() -> None:
    adapter, router, _ = make_adapter(
        {"CancelOrder": _ambiguous("CancelOrder"), "SingleOrdHist": [order_row(status="CANCELED")]}
    )
    assert await adapter.cancel_order("26101500000123") is True
    assert len(router.payloads("CancelOrder")) == 1


async def test_cancel_ambiguous_still_open_raises_with_state() -> None:
    adapter, router, _ = make_adapter(
        {"CancelOrder": _ambiguous("CancelOrder"), "SingleOrdHist": [order_row(status="OPEN")]}
    )
    with pytest.raises(FlattradeError, match="OPEN"):
        await adapter.cancel_order("26101500000123")
    assert len(router.payloads("CancelOrder")) == 1


async def test_cancel_rejected_by_broker_raises() -> None:
    adapter, _, _ = make_adapter({"CancelOrder": {"stat": "Not_Ok", "emsg": "Invalid Input"}})
    with pytest.raises(FlattradeError):
        await adapter.cancel_order("1")


# ── status / book / trades ────────────────────────────────────────────────────


async def test_order_status_uses_last_history_entry() -> None:
    hist = [
        order_row(status="PENDING"),
        order_row(status="OPEN", fillshares="4", avgprc="2499.50"),
    ]
    adapter, _, _ = make_adapter({"SingleOrdHist": hist})
    order = await adapter.get_order_status("26101500000123")
    assert order.status == "PARTIAL"
    assert (order.filled_quantity, order.pending_quantity) == (4, 6)
    assert order.average_price == 2499.50
    assert order.symbol == "RELIANCE"


async def test_order_status_terminal_entry_wins_whatever_the_order() -> None:
    hist = [order_row(status="COMPLETE", fillshares="10", avgprc="2500"), order_row(status="OPEN")]
    adapter, _, _ = make_adapter({"SingleOrdHist": hist})
    order = await adapter.get_order_status("26101500000123")
    assert order.status == "FILLED"
    assert order.filled_quantity == 10


async def test_order_status_conflicting_terminal_entries_fail_loud() -> None:
    hist = [order_row(status="COMPLETE"), order_row(status="REJECTED")]
    adapter, _, _ = make_adapter({"SingleOrdHist": hist})
    with pytest.raises(FlattradeError, match="conflicting"):
        await adapter.get_order_status("26101500000123")


def _replaced_hist() -> list[dict[str, Any]]:
    """Oldest-first: created at 2500, then modified to 2490 (review I5)."""
    return [
        order_row(rpt="NewAck", prc="2500.00", norentm="10:59:00 15-10-2026"),
        order_row(rpt="Replaced", prc="2490.00", norentm="10:59:30 15-10-2026"),
    ]


@pytest.mark.parametrize(
    "hist",
    [
        _replaced_hist(),  # oldest first
        list(reversed(_replaced_hist())),  # newest first (Noren clients' usual order)
        [{**r, "rpt": ""} for r in reversed(_replaced_hist())],  # no rpt: norentm decides
    ],
)
async def test_order_status_takes_the_newest_row_whichever_end_it_is(
    hist: list[dict[str, Any]],
) -> None:
    adapter, _, _ = make_adapter({"SingleOrdHist": hist})
    order = await adapter.get_order_status("26101500000123")
    assert order.raw["prc"] == "2490.00"


async def test_modify_uses_newest_state_when_history_is_newest_first() -> None:
    hist = list(reversed(_replaced_hist()))
    adapter, router, _ = make_adapter(
        {"SingleOrdHist": hist, "ModifyOrder": {"stat": "Ok", "result": "X"}}
    )
    await adapter.modify_order("26101500000123", {"quantity": 20})
    (p,) = router.payloads("ModifyOrder")
    assert p["prc"] == "2490.00"  # not the stale creation price


async def test_order_status_skips_a_rejected_modify_report() -> None:
    hist = [
        order_row(rpt="ModRejected", status="REJECTED", prc="2400.00"),
        order_row(rpt="NewAck", status="OPEN"),
    ]
    adapter, _, _ = make_adapter({"SingleOrdHist": hist})
    order = await adapter.get_order_status("26101500000123")
    assert order.status == "OPEN"
    assert order.raw["prc"] == "2500.00"


async def test_order_status_undetectable_order_with_different_prices_fails_loud() -> None:
    hist = [order_row(prc="2500.00"), order_row(prc="2490.00")]  # no rpt, no norentm
    adapter, _, _ = make_adapter({"SingleOrdHist": hist})
    with pytest.raises(FlattradeError, match="newest"):
        await adapter.get_order_status("26101500000123")


@pytest.mark.parametrize("reply", [[], {"stat": "Not_Ok", "emsg": "No Data :  "}])
async def test_order_status_unknown_order_raises_key_error(reply: Any) -> None:
    adapter, _, _ = make_adapter({"SingleOrdHist": reply})
    with pytest.raises(KeyError):
        await adapter.get_order_status("nope")


async def test_get_all_orders_normalises_rows_and_no_data_is_empty() -> None:
    adapter, _, _ = make_adapter(
        {"OrderBook": [order_row(), order_row(norenordno="2", status="REJECTED", rejreason="RMS")]}
    )
    orders = await adapter.get_all_orders()
    assert [o.status for o in orders] == ["OPEN", "REJECTED"]
    assert orders[1].rejection_reason == "RMS"
    adapter2, _, _ = make_adapter({"OrderBook": {"stat": "Not_Ok", "emsg": "No Data :  "}})
    assert await adapter2.get_all_orders() == []


async def test_get_all_orders_other_error_raises() -> None:
    adapter, _, _ = make_adapter({"OrderBook": {"stat": "Not_Ok", "emsg": "Server Timeout :  "}})
    with pytest.raises(FlattradeError):
        await adapter.get_all_orders()


async def test_get_all_orders_object_reply_raises() -> None:
    adapter, _, _ = make_adapter({"OrderBook": {"norenordno": "1"}})
    with pytest.raises(FlattradeError, match="array"):
        await adapter.get_all_orders()


async def test_get_trades_filters_by_order_number() -> None:
    book = [
        {
            "norenordno": "A",
            "flid": "F1",
            "flqty": "4",
            "flprc": "2499.50",
            "fltm": "15-10-2026 10:59:58",
            "tsym": "RELIANCE-EQ",
            "trantype": "B",
            "prd": "I",
            "exch": "NSE",
            "token": "2885",
        },
        {
            "norenordno": "B",
            "flid": "F2",
            "flqty": "1",
            "flprc": "1",
            "fltm": "15-10-2026 10:59:59",
        },
        {"norenordno": "A", "flid": "F3", "flqty": "6", "flprc": "2500.00", "fltm": "garbage"},
    ]
    adapter, _, _ = make_adapter({"TradeBook": book})
    fills = await adapter.get_trades("A")
    assert [(f.trade_id, f.quantity, f.price) for f in fills] == [
        ("F1", 4, 2499.5),
        ("F3", 6, 2500.0),
    ]
    assert fills[0].exchange_time.hour == 10
    adapter2, _, _ = make_adapter({"TradeBook": {"stat": "Not_Ok", "emsg": "No Data :  "}})
    assert await adapter2.get_trades("A") == []


# ── live orders gate (review C1) ──────────────────────────────────────────────


async def test_live_orders_are_off_by_default_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from pathlib import Path

    from src.brokers.adapters.flattrade.adapter import FlattradeLiveOrdersDisabled

    matrix = Path(adapter_mod.__file__).parents[2] / "capability_matrix.json"
    shipped = json.loads(matrix.read_text(encoding="utf-8"))["flattrade"]
    assert shipped["live_orders_verified"] is False
    monkeypatch.setitem(adapter_mod._CAPS, "live_orders_verified", False)
    adapter, router, _ = make_adapter(
        {
            "GetQuotes": quote("2500"),
            "PlaceOrder": _ok_place,
            "ModifyOrder": {"stat": "Ok"},
            "CancelOrder": {"stat": "Ok"},
            "SingleOrdHist": [order_row()],
        }
    )
    for coro in (
        adapter.place_order(_req(order_type="MARKET", price=None)),
        adapter.modify_order("26101500000123", {"price": 2490.0}),
        adapter.cancel_order("26101500000123"),
    ):
        with pytest.raises(FlattradeLiveOrdersDisabled, match="live_orders_verified") as ei:
            await coro
        assert isinstance(ei.value, NotImplementedError)
        assert isinstance(ei.value, FlattradeError)
    assert router.calls == []


# ── order margin (gated) ──────────────────────────────────────────────────────


async def test_order_margin_gated_before_any_validation_or_request() -> None:
    adapter, router, _ = make_adapter({})
    with pytest.raises(NotImplementedError, match="margin_api_verified"):
        await adapter.get_order_margin([])
    assert router.calls == []


@pytest.fixture
def _margin_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(adapter_mod._CAPS, "margin_api_verified", True)


@pytest.mark.usefixtures("_margin_verified")
async def test_order_margin_request_shape_and_result() -> None:
    reply = {"stat": "Ok", "marginused": "152340.50", "marginusedtrade": "150000", "remarks": ""}
    adapter, router, limiter = make_adapter(
        {"GetBasketMargin": reply, "GetQuotes": quote("101.00", exch="NFO", token="62354")}
    )
    legs = [
        _req(
            symbol="NIFTY29DEC26P21050",
            exchange="NFO",
            direction="SELL",
            quantity=65,
            product="MARGIN",
            price=101.0,
        ),
        _req(
            symbol="NIFTY29DEC26P21050",
            exchange="NFO",
            direction="BUY",
            quantity=65,
            product="MARGIN",
            order_type="SL",
            price=150.0,
            trigger_price=149.0,
        ),
    ]
    result = await adapter.get_order_margin(legs)
    assert result.total_margin == 152340.50
    assert result.per_leg == []
    assert result.raw == reply
    (p,) = router.payloads("GetBasketMargin")
    assert (p["exch"], p["tsym"], p["trantype"], p["qty"], p["prd"], p["prctyp"], p["prc"]) == (
        "NFO",
        "NIFTY29DEC26P21050",
        "S",
        "65",
        "M",
        "LMT",
        "101.00",
    )
    assert "trgprc" not in p
    (leg2,) = p["basketlists"]
    assert (leg2["trantype"], leg2["prctyp"], leg2["trgprc"], leg2["prc"]) == (
        "B",
        "SL-LMT",
        "149.00",
        "150.00",
    )
    assert router.payloads("PlaceOrder") == []
    assert "order" not in limiter.categories


@pytest.mark.usefixtures("_margin_verified")
async def test_order_margin_empty_list_is_value_error() -> None:
    adapter, router, _ = make_adapter({})
    with pytest.raises(ValueError):
        await adapter.get_order_margin([])
    assert router.calls == []


@pytest.mark.usefixtures("_margin_verified")
@pytest.mark.parametrize(
    "reply",
    [
        {"stat": "Ok", "marginused": "100", "remarks": "Insufficient Balance"},
        {"stat": "Ok", "remarks": ""},
        {"stat": "Not_Ok", "emsg": "Invalid Input"},
    ],
)
async def test_order_margin_rejection_or_missing_total_raises(reply: dict[str, Any]) -> None:
    adapter, _, _ = make_adapter({"GetBasketMargin": reply, "GetQuotes": quote("2500")})
    with pytest.raises(FlattradeError):
        await adapter.get_order_margin([_req()])


# ── GTT / capabilities ────────────────────────────────────────────────────────


async def test_gtt_methods_not_implemented_and_capabilities_false() -> None:
    adapter, router, _ = make_adapter({})
    gtt = GTTRequest(
        "RELIANCE", "DELIVERY", "TWO_LEG", 1, "T1", 2400.0, "LIMIT", 2399.0, 2600.0, 2600.0
    )
    for coro in (
        adapter.place_gtt(gtt),
        adapter.modify_gtt("1", {}),
        adapter.cancel_gtt("1"),
        adapter.get_all_gtts(),
    ):
        with pytest.raises(NotImplementedError, match="UNVERIFIED"):
            await coro
    assert router.calls == []
    assert adapter.supports_gtt() is False
    assert adapter.supports_bracket_order() is False
    assert adapter.supports_cover_order() is False
