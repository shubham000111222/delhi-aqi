"""Money, models, clock and store: the foundations everything else rests on."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.clock import SimClock, floor_ts, timeframe_seconds, utc
from tradebot.core.models import (
    BookLevel,
    Candle,
    Order,
    OrderBook,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from tradebot.core.money import D, round_price, round_qty, to_str
from tradebot.core.store import Store


class TestMoney:
    def test_decimal_never_contaminates(self):
        assert D("0.1") + D("0.2") == D("0.3")
        assert str(D(0.1)) == "0.1"

    def test_numpy_scalar_conversion(self):
        import numpy as np

        assert D(np.float64(1.25)) == D("1.25")
        assert D(np.int64(3)) == Decimal("3")

    def test_round_qty_always_rounds_down(self):
        assert round_qty("0.001999", "0.00001") == D("0.00199")
        assert round_qty("1.2345", "0.01") == D("1.23")

    def test_round_price_is_passive_side(self):
        assert round_price("100.019", "0.01", "buy") == D("100.01")
        assert round_price("100.011", "0.01", "sell") == D("100.02")
        assert round_price("100.015", "0.01") == D("100.02")

    def test_to_str(self):
        assert to_str(D("1.5"), 2) == "1.50"
        assert to_str(D("1000000")) == "1000000"

    def test_bool_is_rejected(self):
        with pytest.raises(TypeError):
            D(True)


class TestOrderBook:
    def test_mid_and_spread(self, book: OrderBook):
        assert book.best_bid == D("59999.9")
        assert book.best_ask == D("60000.1")
        assert book.mid == D("60000.0")
        assert D("0") < book.spread_pct < D("0.001")

    def test_walk_moves_price_with_size(self, book: OrderBook):
        avg, filled, worst = book.walk(Side.BUY, D("0.75"))
        assert filled == D("0.75")
        assert avg > book.best_ask  # paid above the touch, i.e. real impact
        assert worst == book.asks[1].price

    def test_walk_limited_by_depth(self, book: OrderBook):
        avg, filled, _ = book.walk(Side.BUY, D("100"))
        assert filled == D("10")  # 20 levels x 0.5
        assert avg > 0

    def test_imbalance_bounds(self):
        bids = tuple(BookLevel(D(100 - i), D("1")) for i in range(5))
        asks = tuple(BookLevel(D(101 + i), D("3")) for i in range(5))
        b = OrderBook(symbol="X", ts=datetime.now(UTC), bids=bids, asks=asks)
        assert D("-1") <= b.imbalance() <= D("1")
        assert b.imbalance() < 0


class TestClientOrderIds:
    def test_same_intent_same_id(self):
        a = OrderRequest(symbol="BTC/USDT", side=Side.BUY, qty=D("1"), type=OrderType.LIMIT, price=D("100"), strategy="s", signal_id="sig")
        b = OrderRequest(symbol="BTC/USDT", side=Side.BUY, qty=D("1"), type=OrderType.LIMIT, price=D("100"), strategy="s", signal_id="sig")
        assert a.client_order_id == b.client_order_id

    def test_different_intent_different_id(self):
        a = OrderRequest(symbol="BTC/USDT", side=Side.SELL, qty=D("1"), type=OrderType.MARKET, strategy="s", signal_id="sig", tag="exit:stop:1")
        b = OrderRequest(symbol="BTC/USDT", side=Side.SELL, qty=D("1"), type=OrderType.MARKET, strategy="s", signal_id="sig", tag="exit:target:2")
        assert a.client_order_id != b.client_order_id


class TestClock:
    def test_sim_clock_only_moves_forward(self):
        clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
        clock.set(datetime(2024, 1, 2, tzinfo=UTC))
        clock.set(datetime(2024, 1, 1, 6, tzinfo=UTC))
        assert clock.now() == datetime(2024, 1, 2, tzinfo=UTC)

    def test_bar_flooring(self):
        ts = datetime(2024, 1, 1, 10, 37, 33, tzinfo=UTC)
        assert floor_ts(ts, 900) == datetime(2024, 1, 1, 10, 30, tzinfo=UTC)
        assert timeframe_seconds("15m") == 900

    def test_naive_datetime_is_treated_as_utc(self):
        assert utc(datetime(2024, 1, 1)).tzinfo is UTC


class TestStore:
    def test_kill_state_survives_reopen(self, tmp_path):
        path = tmp_path / "state.db"
        store = Store(path)
        store.set_kill_state("L2", trigger="test", reason="unit test")
        store.close()

        reopened = Store(path)
        assert reopened.kill_level() == "L2"
        assert reopened.kill_state()["trigger"] == "test"
        events = reopened.events(kinds=["kill_switch"])
        assert events and events[0]["payload"]["level"] == "L2"
        reopened.close()

    def test_events_are_append_only(self):
        store = Store()
        first = store.log_event("x", {"n": 1})
        second = store.log_event("x", {"n": 2})
        assert second > first
        assert [e["payload"]["n"] for e in store.events(limit=10)] == [2, 1]

    def test_orders_upsert_and_open_query(self):
        store = Store()
        order = Order(
            client_order_id="abc",
            symbol="BTC/USDT",
            side=Side.BUY,
            type=OrderType.LIMIT,
            qty=D("0.1"),
            price=D("60000"),
            status=OrderStatus.SUBMITTED,
            created_at=datetime.now(UTC),
        )
        store.upsert_order(order)
        assert len(store.open_orders()) == 1
        order.with_status(OrderStatus.FILLED)
        store.upsert_order(order)
        assert store.open_orders() == []
        assert store.orders()[0]["status"] == "FILLED"

    def test_kv_roundtrip(self):
        store = Store()
        store.kv_set("ramp", {"stage": 2})
        assert store.kv_get("ramp") == {"stage": 2}
        store.kv_delete("ramp")
        assert store.kv_get("ramp", "missing") == "missing"

    def test_holdout_lock(self):
        store = Store()
        assert not store.holdout_locked("S1")
        store.record_holdout_use("S1", run_id="r1")
        assert store.holdout_locked("S1")


class TestCandle:
    def test_close_time(self):
        c = Candle(
            symbol="BTC/USDT",
            timeframe="15m",
            ts=datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
            open=D(1),
            high=D(2),
            low=D(1),
            close=D(2),
            volume=D(10),
        )
        assert c.close_time == datetime(2024, 1, 1, 10, 15, tzinfo=UTC)
        assert c.to_dict()["close"] == "2"


class TestVetoBreakdown:
    """The dashboard's "why were entries refused" table."""

    def _store(self):
        from tradebot.core.store import Store

        return Store(":memory:")

    def _verdict(self, name: str, detail: str = "detail"):
        from tradebot.core.models import RiskCheck, RiskVerdict

        return RiskVerdict(approved=False, reason=f"{name}: {detail}", checks=[RiskCheck(name, False, detail)])

    def test_counts_are_grouped_by_the_failing_check(self):
        from tradebot.dashboard.app import _veto_view

        store = self._store()
        try:
            for _ in range(3):
                store.save_verdict("s1", self._verdict("cooldown"))
            store.save_verdict("s2", self._verdict("cost_budget", "target 20 bps vs 80"))
            view = _veto_view(store)
        finally:
            store.close()
        assert view["available"] is True
        assert view["window"] == 4 and view["refused"] == 4
        assert view["by_check"][0] == {"check": "cooldown", "count": 3, "share": 0.75}
        assert view["by_check"][1]["check"] == "cost_budget"

    def test_the_window_counts_reasons_not_distinct_checks(self):
        from tradebot.dashboard.app import _veto_view

        store = self._store()
        try:
            store.save_verdict("s1", self._verdict("cooldown", "cooldown until 10:00"))
            store.save_verdict("s2", self._verdict("cooldown", "cooldown until 11:00"))
            view = _veto_view(store)
        finally:
            store.close()
        assert view["by_check"] == [{"check": "cooldown", "count": 2, "share": 1.0}]

    def test_approved_entries_are_counted_but_not_listed_as_reasons(self):
        from tradebot.core.models import RiskVerdict
        from tradebot.dashboard.app import _veto_view

        store = self._store()
        try:
            store.save_verdict("s1", RiskVerdict(approved=True, reason="approved", checks=[]))
            store.save_verdict("s2", self._verdict("spread"))
            view = _veto_view(store)
        finally:
            store.close()
        assert view["approved"] == 1 and view["refused"] == 1
        assert view["approval_rate"] == 0.5
        assert [row["check"] for row in view["by_check"]] == ["spread"]

    def test_only_the_top_ten_reasons_are_returned(self):
        from tradebot.dashboard.app import _veto_view

        store = self._store()
        try:
            for index in range(14):
                store.save_verdict(f"s{index}", self._verdict(f"check_{index}"))
            view = _veto_view(store)
        finally:
            store.close()
        assert len(view["by_check"]) == 10
        assert view["window"] == 14

    def test_an_empty_log_says_so_instead_of_showing_zeroes(self):
        from tradebot.dashboard.app import _veto_view

        store = self._store()
        try:
            view = _veto_view(store)
        finally:
            store.close()
        assert view["available"] is False and "no risk verdicts" in view["note"]

    def test_an_unreadable_store_is_reported_not_raised(self):
        from tradebot.dashboard.app import _veto_view

        class Broken:
            def verdicts(self, limit: int = 10):
                raise RuntimeError("database is locked")

        assert _veto_view(Broken())["available"] is False
