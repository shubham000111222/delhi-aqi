"""The live ccxt adapter, exercised through a fake exchange.

The adapter cannot be run against a real venue from this repository (no exchange egress,
no keys), and untested code on a money path is the worst kind. So everything between the
bot and the network is tested here against a fake that behaves like a ccxt exchange: the
same method names, the same shapes of dictionary, and the same exceptions. What is *not*
covered is ccxt's own HTTP layer and the venue's real behaviour - the module says so.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from tradebot.config import Mode, load_config
from tradebot.core.clock import Clock
from tradebot.core.models import KillLevel, OrderRequest, OrderStatus, OrderType, Side
from tradebot.core.store import Store
from tradebot.data.hub import MarketData
from tradebot.execution.adapters.base import PermanentExchangeError, TransientExchangeError
from tradebot.execution.adapters.ccxt_live import CcxtLiveAdapter
from tradebot.risk.killswitch import KillSwitch

SYMBOL = "BTC/USDT"


class FakeCcxtError(Exception):
    """A ccxt-shaped error: the adapter classifies by exception *name*."""


class RateLimitExceeded(FakeCcxtError):
    pass


class NetworkError(FakeCcxtError):
    pass


class OrderNotFound(FakeCcxtError):
    pass


class BadSymbol(FakeCcxtError):
    pass


MARKETS: dict[str, dict[str, Any]] = {
    SYMBOL: {
        "limits": {"amount": {"min": 0.0001}, "cost": {"min": 5.0}},
        "precision": {"price": 2, "amount": 6},
        "maker": 0.0002,
        "taker": 0.0004,
    },
    "ETH/USDT": {
        "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}},
        "precision": {"price": 2, "amount": 4},
        "maker": 0.0002,
        "taker": 0.0004,
    },
}


class FakeExchange:
    """Just enough of the ccxt async interface to run the adapter's real code paths."""

    def __init__(self, *, fail_on_create: Exception | None = None, symbols: list[str] | None = None) -> None:
        # every configured symbol must exist, or connect() refuses - which is the point
        self.markets = {
            symbol: MARKETS.get(symbol, MARKETS[SYMBOL]) for symbol in (symbols or list(MARKETS))
        }
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.created: list[dict[str, Any]] = []
        self.order_state: dict[str, dict[str, Any]] = {}
        self.fail_on_create = fail_on_create
        self.balance: dict[str, Any] = {"free": {"USDT": 250.0, "BTC": 0.01}, "total": {}}
        self.permissions: Any = {"info": {"enableWithdrawals": False, "ipRestrict": True}}
        self.book: dict[str, Any] = {
            "bids": [[44000.0, 0.5], [43990.0, 1.0]],
            "asks": [[44010.0, 0.4], [44020.0, 0.9]],
        }
        self.closed = False
        self.sandbox = False

    async def load_markets(self) -> dict[str, Any]:
        self.calls.append(("load_markets", (), {}))
        return self.markets

    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox = enabled

    async def fetch_permissions(self) -> Any:
        if isinstance(self.permissions, Exception):
            raise self.permissions
        return self.permissions

    async def create_order(
        self, symbol: str, type: str, side: str, amount: float, price: float | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("create_order", (symbol, type, side, amount, price), dict(params)))
        if self.fail_on_create is not None:
            raise self.fail_on_create
        order_id = f"x{len(self.created) + 1}"
        self.created.append(
            {"symbol": symbol, "type": type, "side": side, "amount": amount, "price": price, "params": params}
        )
        state = {"status": "open", "filled": 0.0, "average": None, "id": order_id}
        self.order_state[order_id] = state
        return dict(state, fee={"cost": 0.0})

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self.calls.append(("cancel_order", (order_id, symbol), {}))
        if order_id not in self.order_state:
            raise OrderNotFound("unknown order")
        self.order_state[order_id]["status"] = "canceled"
        return self.order_state[order_id]

    async def fetch_order(self, order_id: str, symbol: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("fetch_order", (order_id, symbol), dict(params)))
        if order_id not in self.order_state:
            raise OrderNotFound("unknown order")
        return dict(self.order_state[order_id])

    async def fetch_balance(self) -> dict[str, Any]:
        self.calls.append(("fetch_balance", (), {}))
        return self.balance

    async def fetch_order_book(self, symbol: str, levels: int) -> dict[str, Any]:
        self.calls.append(("fetch_order_book", (symbol, levels), {}))
        return self.book

    async def close(self) -> None:
        self.closed = True


def build(exchange: FakeExchange) -> CcxtLiveAdapter:
    cfg = load_config()
    cfg = replace(cfg, settings=cfg.settings.model_copy(update={"mode": Mode.LIVE}))
    market = MarketData(cfg.symbol_names(), timeframes=())
    return CcxtLiveAdapter(cfg, market, Clock(), exchange=exchange,
                           killswitch=KillSwitch(Store(":memory:"), kill_file=None, mode=Mode.LIVE))


def adapter_with(**kwargs: Any) -> tuple[CcxtLiveAdapter, FakeExchange]:
    exchange = FakeExchange(symbols=load_config().symbol_names(), **kwargs)
    adapter = build(exchange)
    asyncio.run(adapter.connect())
    return adapter, exchange


def request(**overrides: Any) -> OrderRequest:
    payload: dict[str, Any] = {
        "symbol": SYMBOL,
        "side": Side.BUY,
        "qty": Decimal("0.00331234"),
        "type": OrderType.LIMIT,
        "price": Decimal("44000.567"),
        "client_order_id": "tb-abc123",
        "strategy": "S1_ema_trend",
        "signal_id": "sig-1",
        "sized_ramp_multiplier": Decimal("0.10"),
    }
    payload.update(overrides)
    return OrderRequest(**payload)


class TestConnect:
    def test_rules_come_from_the_venue_not_from_the_adapter(self):
        adapter, _ = adapter_with()
        rules = adapter.get_market_rules(SYMBOL)
        assert rules.tick_size == Decimal("0.01")
        assert rules.min_qty == Decimal("0.0001")
        assert rules.min_notional == Decimal("5")
        assert rules.taker_fee == Decimal("0.0004")

    def test_a_symbol_the_venue_does_not_list_stops_the_connect(self):
        cfg = load_config()
        exchange = FakeExchange(symbols=[SYMBOL])
        adapter = CcxtLiveAdapter(cfg, MarketData(cfg.symbol_names(), timeframes=()), Clock(), exchange=exchange)
        with pytest.raises(ValueError, match="not listed"):
            asyncio.run(adapter.connect())

    def test_unknown_rules_raise_rather_than_guessing(self):
        adapter, _ = adapter_with()
        with pytest.raises(KeyError):
            adapter.get_market_rules("DOGE/USDT")


class TestOrders:
    def test_a_limit_order_carries_its_client_id_and_rounded_numbers(self):
        adapter, exchange = adapter_with()
        order = asyncio.run(adapter.place_order(request()))
        assert order.exchange_order_id == "x1"
        assert order.qty == Decimal("0.003312"), "qty rounded to the lot step"
        assert order.price == Decimal("44000.56"), "a buy price rounds DOWN, never up"
        method, args, params = exchange.calls[-1]
        assert method == "create_order"
        assert args[0] == SYMBOL and args[1] == "limit" and args[2] == "buy"
        assert params["newClientOrderId"] == "tb-abc123", "retries must not duplicate"

    def test_a_sell_price_rounds_up_so_the_bot_never_asks_for_less(self):
        adapter, _ = adapter_with()
        order = asyncio.run(adapter.place_order(request(side=Side.SELL)))
        assert order.price == Decimal("44000.57")

    def test_the_same_client_id_is_never_sent_twice(self):
        adapter, exchange = adapter_with()
        asyncio.run(adapter.place_order(request()))
        again = asyncio.run(adapter.place_order(request()))
        assert again.exchange_order_id == "x1"
        assert len([c for c in exchange.calls if c[0] == "create_order"]) == 1

    def test_a_limit_order_without_a_price_is_refused_before_any_call(self):
        adapter, exchange = adapter_with()
        with pytest.raises(PermanentExchangeError, match="requires a price"):
            asyncio.run(adapter.place_order(request(price=None)))
        assert not [c for c in exchange.calls if c[0] == "create_order"]

    def test_a_venue_error_is_translated_not_leaked(self):
        adapter, _ = adapter_with(fail_on_create=RateLimitExceeded("too many requests"))
        with pytest.raises(TransientExchangeError):
            asyncio.run(adapter.place_order(request()))

    def test_an_unknown_error_is_permanent(self):
        adapter, _ = adapter_with(fail_on_create=BadSymbol("no such symbol"))
        with pytest.raises(PermanentExchangeError):
            asyncio.run(adapter.place_order(request()))

    def test_a_stop_order_carries_its_trigger(self):
        adapter, exchange = adapter_with()
        asyncio.run(
            adapter.place_order(
                request(type=OrderType.STOP_MARKET, price=None, stop_price=Decimal("43000"))
            )
        )
        _, args, params = exchange.calls[-1]
        assert args[1] == "stop_market"
        assert params["stopPrice"] == 43000.0

    def test_cancel_marks_the_order_and_reports_success(self):
        adapter, _ = adapter_with()
        order = asyncio.run(adapter.place_order(request()))
        assert asyncio.run(adapter.cancel_order(order.client_order_id)) is True
        assert adapter.orders[order.client_order_id].status is OrderStatus.CANCELED

    def test_cancelling_something_unknown_is_false_not_an_error(self):
        adapter, _ = adapter_with()
        assert asyncio.run(adapter.cancel_order("nope")) is False

    def test_cancel_all_only_touches_open_orders(self):
        adapter, _ = adapter_with()
        asyncio.run(adapter.place_order(request()))
        asyncio.run(adapter.place_order(request(client_order_id="tb-2")))
        assert asyncio.run(adapter.cancel_all()) == 2
        assert asyncio.run(adapter.cancel_all()) == 0


class TestFillDetection:
    def test_a_partial_fill_is_emitted_once_and_only_for_the_delta(self):
        adapter, exchange = adapter_with()
        asyncio.run(adapter.place_order(request()))
        fills: list[Any] = []
        adapter.on_fill = fills.append

        exchange.order_state["x1"].update({"status": "partially_filled", "filled": 0.001, "average": 44000.5})
        asyncio.run(adapter.drain_pending())
        assert len(fills) == 1
        assert fills[0].qty == Decimal("0.001")
        assert fills[0].price == Decimal("44000.5")

        asyncio.run(adapter.drain_pending())
        assert len(fills) == 1, "the same fill must not be counted twice"

        exchange.order_state["x1"].update({"status": "closed", "filled": 0.003312, "average": 44000.5})
        asyncio.run(adapter.drain_pending())
        assert len(fills) == 2
        assert fills[1].qty == Decimal("0.002312"), "only the new quantity"

    def test_the_fee_comes_from_the_venue_taker_fee(self):
        adapter, exchange = adapter_with()
        asyncio.run(adapter.place_order(request()))
        fills: list[Any] = []
        adapter.on_fill = fills.append
        exchange.order_state["x1"].update({"status": "closed", "filled": 0.01, "average": 10000.0})
        asyncio.run(adapter.drain_pending())
        assert fills[0].fee == Decimal("0.01") * Decimal("10000.0") * adapter.get_market_rules(SYMBOL).taker_fee

    def test_a_polling_error_does_not_stop_the_other_orders(self):
        adapter, exchange = adapter_with()
        asyncio.run(adapter.place_order(request()))
        asyncio.run(adapter.place_order(request(client_order_id="tb-2")))

        original = exchange.fetch_order

        async def flaky(order_id: str, symbol: str, params: dict[str, Any]) -> dict[str, Any]:
            if order_id == "x1":
                raise NetworkError("socket closed")
            return await original(order_id, symbol, params)

        exchange.fetch_order = flaky  # type: ignore[method-assign]
        exchange.order_state["x2"].update({"status": "closed", "filled": 0.001, "average": 44000.0})
        fills: list[Any] = []
        adapter.on_fill = fills.append
        asyncio.run(adapter.drain_pending())
        assert len(fills) == 1, "one broken order must not hide the other's fill"


class TestStatusAndAccounts:
    def test_status_mapping(self):
        adapter, exchange = adapter_with()
        order = asyncio.run(adapter.place_order(request()))
        exchange.order_state["x1"].update({"status": "closed", "filled": 0.003312, "average": 44000.0})
        fresh = asyncio.run(adapter.get_order(order.client_order_id))
        assert fresh is not None and fresh.status is OrderStatus.FILLED
        exchange.order_state["x1"]["status"] = "rejected"
        fresh = asyncio.run(adapter.get_order(order.client_order_id))
        assert fresh is not None and fresh.status is OrderStatus.REJECTED

    def test_an_unknown_order_is_none(self):
        adapter, _ = adapter_with()
        assert asyncio.run(adapter.get_order("missing")) is None

    def test_balances_are_the_free_amounts(self):
        adapter, _ = adapter_with()
        balances = asyncio.run(adapter.get_balances())
        assert balances["USDT"] == Decimal("250.0")
        assert balances["BTC"] == Decimal("0.01")

    def test_market_rules_are_loaded_per_symbol(self):
        adapter, _ = adapter_with()
        assert adapter.get_market_rules("ETH/USDT").lot_step == Decimal("0.0001")


class TestPermissions:
    def test_withdrawals_disabled_and_ip_restricted_passes(self):
        adapter, _ = adapter_with()
        result = asyncio.run(adapter.verify_permissions())
        assert result["passed"] is True

    def test_withdrawals_enabled_fails_the_check(self):
        adapter, exchange = adapter_with()
        exchange.permissions = {"info": {"enableWithdrawals": True, "ipRestrict": True}}
        result = asyncio.run(adapter.verify_permissions())
        assert result["passed"] is False
        assert "withdrawals are ENABLED" in result["detail"]

    def test_unverifiable_permissions_fail_closed(self):
        adapter, exchange = adapter_with()
        exchange.permissions = {"info": {}}
        result = asyncio.run(adapter.verify_permissions())
        assert result["passed"] is False

    def test_a_permission_api_that_raises_fails_closed(self):
        adapter, exchange = adapter_with()
        exchange.permissions = NetworkError("timeout")
        result = asyncio.run(adapter.verify_permissions())
        assert result["passed"] is False
        assert "unavailable" in result["detail"]


class TestMarketData:
    def test_the_book_is_parsed_into_decimal_levels(self):
        adapter, _ = adapter_with()
        book = asyncio.run(adapter.refresh_book(SYMBOL))
        assert book is not None
        assert book.best_bid == Decimal("44000.0")
        assert book.best_ask == Decimal("44010.0")

    def test_an_empty_book_is_none_not_a_zero_priced_book(self):
        adapter, exchange = adapter_with()
        exchange.book = {"bids": [], "asks": []}
        assert asyncio.run(adapter.refresh_book(SYMBOL)) is None

    def test_close_closes_the_venue_client(self):
        adapter, exchange = adapter_with()
        asyncio.run(adapter.close())
        assert exchange.closed is True


def test_live_adapter_refuses_direct_entry_without_killswitch():
    cfg = load_config()
    cfg = replace(cfg, settings=cfg.settings.model_copy(update={"mode": Mode.LIVE}))
    exchange = FakeExchange(symbols=cfg.symbol_names())
    adapter = CcxtLiveAdapter(cfg, MarketData(cfg.symbol_names(), timeframes=()), Clock(), exchange=exchange)
    asyncio.run(adapter.connect())
    with pytest.raises(PermanentExchangeError, match="not attached"):
        asyncio.run(adapter.place_order(request()))
    assert not [call for call in exchange.calls if call[0] == "create_order"]


@pytest.mark.parametrize("level", [KillLevel.L1, KillLevel.L2, KillLevel.L3])
def test_live_adapter_direct_entry_obeys_persisted_kill_level(level):
    adapter, exchange = adapter_with()
    assert adapter.killswitch is not None
    adapter.killswitch.trigger(level, trigger="test", reason="pause")
    with pytest.raises(PermanentExchangeError, match="blocks entry"):
        asyncio.run(adapter.place_order(request()))
    assert not [call for call in exchange.calls if call[0] == "create_order"]
    assert adapter.killswitch.store.count_events("autonomy_refused") == 1


def test_live_adapter_rejects_buy_disguised_as_reduce_only():
    adapter, exchange = adapter_with()
    assert adapter.killswitch is not None
    adapter.killswitch.trigger(KillLevel.L1, trigger="test", reason="pause")
    with pytest.raises(PermanentExchangeError, match="reduce-only buy"):
        asyncio.run(adapter.place_order(request(reduce_only=True)))
    assert not [call for call in exchange.calls if call[0] == "create_order"]
    assert adapter.killswitch.store.count_events("autonomy_refused") == 1


def test_live_adapter_keeps_protective_sell_available_at_l3():
    adapter, exchange = adapter_with()
    assert adapter.killswitch is not None
    adapter.killswitch.trigger(KillLevel.L3, trigger="test", reason="pause")
    asyncio.run(adapter.place_order(request(side=Side.SELL, reduce_only=True)))
    assert len([call for call in exchange.calls if call[0] == "create_order"]) == 1


def test_live_adapter_checks_kill_file_for_direct_entry(tmp_path):
    adapter, exchange = adapter_with()
    assert adapter.killswitch is not None
    kill_file = tmp_path / "KILL"
    kill_file.write_text("emergency stop")
    adapter.killswitch.kill_file = kill_file
    with pytest.raises(PermanentExchangeError, match="blocks entry"):
        asyncio.run(adapter.place_order(request()))
    assert adapter.killswitch.level is KillLevel.L2
    assert not [call for call in exchange.calls if call[0] == "create_order"]


def test_live_adapter_fails_closed_if_kill_state_cannot_be_read(monkeypatch):
    adapter, exchange = adapter_with()
    assert adapter.killswitch is not None

    def broken():
        raise OSError("database unavailable")

    monkeypatch.setattr(adapter.killswitch, "can_open_entries", broken)
    with pytest.raises(OSError, match="database unavailable"):
        asyncio.run(adapter.place_order(request()))
    assert not [call for call in exchange.calls if call[0] == "create_order"]


@pytest.mark.parametrize("mutation", ["mode", "adapter_mode", "risk"])
def test_live_adapter_refuses_direct_entries_after_runtime_changes(mutation):
    adapter, exchange = adapter_with()
    if mutation == "mode":
        adapter.cfg.settings.mode = Mode.PAPER
    elif mutation == "adapter_mode":
        adapter.mode = Mode.PAPER
    else:
        adapter.cfg = replace(adapter.cfg, risk=adapter.cfg.risk.model_copy(update={"risk_per_trade": Decimal("0.02")}))
    with pytest.raises(PermanentExchangeError, match="configuration"):
        asyncio.run(adapter.place_order(request()))
    assert not [call for call in exchange.calls if call[0] == "create_order"]


def test_live_adapter_refuses_kill_switch_with_wrong_mode():
    adapter, exchange = adapter_with()
    adapter.killswitch = KillSwitch(Store(":memory:"), kill_file=None, mode=Mode.PAPER)
    with pytest.raises(PermanentExchangeError, match="kill switch changed"):
        asyncio.run(adapter.place_order(request()))
    assert not [call for call in exchange.calls if call[0] == "create_order"]
    adapter.killswitch.store.close()


def test_direct_live_adapter_refuses_disabled_strategy():
    adapter, exchange = adapter_with()
    store = adapter.killswitch.store
    store.disable_strategy("S1", "fault", faults=3, ts=adapter.clock.now())
    order = replace(request(), strategy="S1")
    with pytest.raises(PermanentExchangeError, match="durably disabled"):
        asyncio.run(adapter.place_order(order))
    assert not [call for call in exchange.calls if call[0] == "create_order"]
    store.close()


@pytest.mark.parametrize("multiplier", [None, Decimal("NaN"), Decimal("0"), Decimal("0.10")])
def test_direct_live_entry_refuses_stale_sizing(multiplier):
    adapter, exchange = adapter_with()
    store = adapter.killswitch.store
    store.kv_set("operator_allocation_cap", {"cap": "0.05"})
    with pytest.raises(PermanentExchangeError, match="sizing multiplier"):
        asyncio.run(adapter.place_order(request(sized_ramp_multiplier=multiplier)))
    assert not [call for call in exchange.calls if call[0] == "create_order"]
    store.close()


def test_live_protective_sell_does_not_require_sizing_assertion():
    adapter, exchange = adapter_with()
    store = adapter.killswitch.store
    store.kv_set("operator_allocation_cap", {"cap": "invalid"})
    order = asyncio.run(adapter.place_order(request(side=Side.SELL, reduce_only=True, sized_ramp_multiplier=None)))
    assert order.reduce_only
    assert [call for call in exchange.calls if call[0] == "create_order"]
    store.close()


def test_cap_reduction_between_manager_and_adapter_blocks_venue_call():
    from tradebot.core.models import Signal
    from tradebot.execution.order_manager import OrderManager

    adapter, exchange = adapter_with()
    store = adapter.killswitch.store
    manager = OrderManager(adapter.cfg, adapter, store, adapter.killswitch, adapter.clock)
    original = adapter.place_order

    async def reduce_before_adapter(request):
        store.kv_set("operator_allocation_cap", {"cap": "0.05"})
        return await original(request)

    adapter.place_order = reduce_before_adapter
    signal = Signal(id="cap-race", strategy="S1", symbol=SYMBOL, timeframe="15m", side=Side.BUY,
                    entry_type=OrderType.MARKET, ts=adapter.clock.now(),
                    stop=Decimal("43000"), target=Decimal("46000"))
    result = asyncio.run(manager.submit_entry(signal, Decimal("0.001"), price=Decimal("44000"),
                                              sized_ramp_multiplier=Decimal("0.10")))
    assert result is None
    assert not [call for call in exchange.calls if call[0] == "create_order"]
    assert store.count_events("order_rejected") == 1
    store.close()
