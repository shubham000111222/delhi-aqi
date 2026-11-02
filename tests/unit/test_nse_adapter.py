"""The NSE/Kite adapter, driven through an httpx MockTransport.

No broker account, no network: every request and every Kite response here is a fixture.
What is being tested is the part that loses money when it is wrong - order shaping,
local rule checks, idempotency by tag, status mapping and the closed-market refusal.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from tradebot.core.clock import SimClock
from tradebot.core.models import OrderRequest, OrderStatus, OrderType, Side
from tradebot.execution.adapters.base import PermanentExchangeError, TransientExchangeError
from tradebot.execution.adapters.nse import KiteAdapter
from tradebot.venues.calendar import NSECalendar
from tradebot.venues.instruments import Instrument, Product

# 2024-01-01 is a Monday; 09:45 IST is inside the session.
OPEN_TIME = datetime(2024, 1, 1, 4, 15, tzinfo=UTC)
CLOSED_TIME = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)  # 17:30 IST

INSTRUMENTS = {
    "RELIANCE": Instrument(symbol="RELIANCE", tick_size=Decimal("0.05"), lot_size=Decimal("1")),
    "INFY": Instrument(symbol="INFY", tick_size=Decimal("0.05"), lot_size=Decimal("1")),
}


def adapter(handler: Any, *, at: datetime = OPEN_TIME, **kwargs: Any) -> KiteAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return KiteAdapter(
        api_key="test-key",
        access_token="test-token",
        instruments=INSTRUMENTS,
        client=client,
        clock=SimClock(at),
        calendar=NSECalendar(),
        **kwargs,
    )


def request(*, symbol: str = "RELIANCE", qty: str = "10", price: str | None = "2500.05",
            side: Side = Side.BUY, type_: OrderType = OrderType.LIMIT, tag: str = "") -> OrderRequest:
    return OrderRequest(
        symbol=symbol, side=side, qty=Decimal(qty), type=type_,
        price=Decimal(price) if price else None, client_order_id=tag, strategy="S1",
    )


def ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})


def recorded(handler: Any) -> tuple[KiteAdapter, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    async def wrapped(req: httpx.Request) -> httpx.Response:
        record = {"method": req.method, "path": req.url.path}
        if req.content:
            from urllib.parse import parse_qs

            record["form"] = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        record["params"] = dict(req.url.params)
        record["headers"] = dict(req.headers)
        calls.append(record)
        return await handler(req)

    return adapter(wrapped), calls


class TestOrderShaping:
    def test_a_limit_order_carries_the_right_kite_fields(self):
        async def handler(req: httpx.Request) -> httpx.Response:
            return ok({"order_id": "240101000000001"})

        async def run():
            kite, calls = recorded(handler)
            order = await kite.place_order(request())
            return order, calls

        order, calls = asyncio.run(run())
        place = [call for call in calls if call["method"] == "POST"][-1]
        assert place["path"] == "/orders/regular"
        assert place["form"]["tradingsymbol"] == "RELIANCE"
        assert place["form"]["exchange"] == "NSE"
        assert place["form"]["transaction_type"] == "BUY"
        assert place["form"]["order_type"] == "LIMIT"
        assert place["form"]["product"] == "MIS"
        assert place["form"]["quantity"] == "10"
        assert place["form"]["price"] == "2500.05"
        assert place["headers"]["authorization"] == "token test-key:test-token"
        assert order.exchange_order_id == "240101000000001"
        assert order.status is OrderStatus.SUBMITTED

    def test_a_market_order_sends_no_price_and_a_reduce_only_sell_comes_from_the_side(self):
        async def handler(req: httpx.Request) -> httpx.Response:
            return ok({"order_id": "1"})

        async def run():
            kite, calls = recorded(handler)
            await kite.place_order(request(price=None, type_=OrderType.MARKET, side=Side.SELL))
            return calls

        calls = asyncio.run(run())
        form = [call for call in calls if call["method"] == "POST"][-1]["form"]
        assert form["order_type"] == "MARKET"
        assert form["transaction_type"] == "SELL"
        assert "price" not in form

    def test_a_stop_order_becomes_a_stop_loss_market_with_a_trigger(self):
        async def handler(req: httpx.Request) -> httpx.Response:
            return ok({"order_id": "1"})

        async def run():
            kite, calls = recorded(handler)
            await kite.place_order(
                dataclasses.replace(
                    request(price=None, type_=OrderType.STOP_MARKET), stop_price=Decimal("2400.00")
                )
            )
            return calls

        calls = asyncio.run(run())
        form = [call for call in calls if call["method"] == "POST"][-1]["form"]
        assert form["order_type"] == "SL-M"
        assert form["trigger_price"] == "2400.00"

    def test_lines_from_a_delivery_instrument_use_cnc(self):
        instruments = {"TCS": Instrument(symbol="TCS", product=Product.DELIVERY)}

        async def handler(req: httpx.Request) -> httpx.Response:
            return ok({"order_id": "1"})

        async def run():
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            kite = KiteAdapter(
                api_key="k", access_token="t", instruments=instruments, client=client,
                clock=SimClock(OPEN_TIME), calendar=NSECalendar(),
            )
            await kite.place_order(request(symbol="TCS", price="3500.00"))
            return kite.calls

        assert asyncio.run(run())


class TestLocalRefusals:
    def test_a_quantity_that_is_not_a_lot_multiple_never_reaches_the_broker(self):
        async def handler(req: httpx.Request) -> httpx.Response:
            return ok({"order_id": "1"})

        async def run():
            kite, calls = recorded(handler)
            with pytest.raises(PermanentExchangeError, match="lot"):
                await kite.place_order(request(qty="10.5"))
            return calls

        calls = asyncio.run(run())
        assert not [call for call in calls if call["method"] == "POST"], "a rejected order must not be sent"

    def test_a_price_off_the_tick_is_refused_locally(self):
        async def handler(req: httpx.Request) -> httpx.Response:
            return ok({"order_id": "1"})

        async def run():
            kite, _calls = recorded(handler)
            with pytest.raises(PermanentExchangeError, match="tick"):
                await kite.place_order(request(price="2500.03"))

        asyncio.run(run())

    def test_an_order_outside_the_session_is_a_bug_not_a_retry(self):
        async def handler(req: httpx.Request) -> httpx.Response:
            return ok({"order_id": "1"})

        async def run():
            kite = adapter(handler, at=CLOSED_TIME)
            with pytest.raises(PermanentExchangeError, match="market closed"):
                await kite.place_order(request())

        asyncio.run(run())

    def test_an_unknown_symbol_is_refused_rather_than_guessed(self):
        async def handler(req: httpx.Request) -> httpx.Response:
            return ok({"order_id": "1"})

        async def run():
            kite = adapter(handler)
            with pytest.raises(PermanentExchangeError, match="no instrument rules"):
                await kite.place_order(request(symbol="UNKNOWN"))

        asyncio.run(run())


class TestIdempotency:
    def test_a_live_order_with_the_same_tag_is_reused_not_duplicated(self):
        live = {
            "order_id": "42",
            "tradingsymbol": "RELIANCE",
            "transaction_type": "BUY",
            "order_type": "LIMIT",
            "quantity": 10,
            "filled_quantity": 0,
            "average_price": 0,
            "price": 2500.05,
            "status": "OPEN",
            "tag": "tb123",
        }

        async def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "GET" and req.url.path == "/orders":
                return ok([live])
            return ok({"order_id": "99"})

        async def run():
            kite, calls = recorded(handler)
            order = await kite.place_order(request(tag="tb123"))
            return order, calls

        order, calls = asyncio.run(run())
        assert order.exchange_order_id == "42"
        assert not [call for call in calls if call["method"] == "POST"]

    def test_a_transport_failure_checks_for_the_order_before_reporting_failure(self):
        state = {"placed": False}
        live = {
            "order_id": "77", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
            "order_type": "LIMIT", "quantity": 10, "filled_quantity": 0,
            "average_price": 0, "price": 2500.05, "status": "OPEN", "tag": "tb777",
        }

        async def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                state["placed"] = True
                raise httpx.ReadTimeout("timeout", request=req)
            return ok([live] if state["placed"] else [])

        async def run():
            kite = adapter(handler)
            return await kite.place_order(request(tag="tb777"))

        order = asyncio.run(run())
        assert order.exchange_order_id == "77", "the order landed despite the timeout"

    def test_a_transport_failure_with_no_order_lands_as_transient(self):
        async def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                raise httpx.ConnectError("down", request=req)
            return ok([])

        async def run():
            kite = adapter(handler)
            with pytest.raises(TransientExchangeError):
                await kite.place_order(request())

        asyncio.run(run())


class TestErrors:
    def test_a_rate_limit_is_transient(self):
        async def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"status": "error", "message": "slow down"})

        async def run():
            kite = adapter(handler)
            with pytest.raises(TransientExchangeError):
                await kite.cancel_all()

        asyncio.run(run())

    def test_a_broker_rejection_is_permanent_and_carries_its_message(self):
        async def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"status": "error", "message": "insufficient funds", "error_type": "Margin"}
            )

        async def run():
            kite = adapter(handler)
            with pytest.raises(PermanentExchangeError, match="insufficient funds"):
                await kite.place_order(request())

        asyncio.run(run())


class TestReads:
    def test_open_orders_and_fills_map_onto_our_statuses(self):
        rows = [
            {"order_id": "1", "tradingsymbol": "RELIANCE", "transaction_type": "BUY", "order_type": "LIMIT",
             "quantity": 10, "filled_quantity": 0, "average_price": 0, "price": 2500.05,
             "status": "OPEN", "tag": "a"},
            {"order_id": "2", "tradingsymbol": "INFY", "transaction_type": "SELL", "order_type": "MARKET",
             "quantity": 5, "filled_quantity": 5, "average_price": 1600.1, "price": 0,
             "status": "COMPLETE", "tag": "b"},
        ]

        async def handler(req: httpx.Request) -> httpx.Response:
            return ok(rows)

        async def run():
            kite = adapter(handler)
            return await kite.get_open_orders(), await kite.get_order("b")

        open_orders, filled = asyncio.run(run())
        assert [order.exchange_order_id for order in open_orders] == ["1"]
        assert filled is not None
        assert filled.status is OrderStatus.FILLED
        assert filled.filled_qty == Decimal("5")
        assert filled.avg_fill_price == Decimal("1600.1")
        assert filled.price is None, "a zero price field means no price, not a zero-price order"

    def test_balances_read_the_equity_available_cash(self):
        async def handler(req: httpx.Request) -> httpx.Response:
            return ok({"equity": {"available": {"cash": 250000.5}}})

        async def run():
            return await adapter(handler).get_balances()

        assert asyncio.run(run()) == {"INR": Decimal("250000.5")}

    def test_last_prices_are_cached_for_the_engine(self):
        async def handler(req: httpx.Request) -> httpx.Response:
            return ok({"NSE:RELIANCE": {"last_price": 2510.25}})

        async def run():
            kite = adapter(handler)
            prices = await kite.refresh_prices(["RELIANCE"])
            return prices, kite.market_price("RELIANCE")

        prices, last = asyncio.run(run())
        assert prices == {"RELIANCE": Decimal("2510.25")}
        assert last == Decimal("2510.25")

    def test_cancel_all_skips_orders_it_cannot_cancel(self):
        rows = [
            {"order_id": "1", "tradingsymbol": "RELIANCE", "status": "OPEN"},
            {"order_id": "2", "tradingsymbol": "RELIANCE", "status": "OPEN"},
            {"order_id": "3", "tradingsymbol": "RELIANCE", "status": "COMPLETE"},
        ]
        deleted: list[str] = []

        async def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "DELETE":
                path = req.url.path
                if path.endswith("/1"):
                    deleted.append("1")
                    return ok({"order_id": "1"})
                return httpx.Response(400, json={"status": "error", "message": "already executed"})
            return ok(rows)

        async def run():
            return await adapter(handler).cancel_all()

        assert asyncio.run(run()) == 1
        assert deleted == ["1"]

    def test_the_adapter_describes_itself_honestly(self):
        async def handler(req: httpx.Request) -> httpx.Response:
            return ok([])

        kite = adapter(handler)
        described = kite.describe()
        assert described["exchange"] == "NSE"
        assert described["market_open"] is True
        closed = adapter(handler, at=CLOSED_TIME).describe()
        assert closed["market_open"] is False


class TestConstruction:
    def test_without_keys_it_refuses_to_exist(self):
        with pytest.raises(ValueError, match="API key"):
            KiteAdapter(api_key="", access_token="")

    def test_it_can_be_built_from_config_secrets(self):
        class Secrets:
            kite_api_key = "k"
            kite_access_token = "t"

        class Config:
            secrets = Secrets()

        kite = KiteAdapter.from_config(Config())
        assert kite.api_key == "k"
        assert json.dumps(kite.describe())  # describable without a network
