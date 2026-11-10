"""Paper exchange realism, order manager retries, ledger invariants and sizing caps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.conftest import make_book

from tradebot.config import Mode
from tradebot.core.clock import SimClock
from tradebot.core.models import (
    Fill,
    KillLevel,
    MarketRules,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Signal,
    Trade,
)
from tradebot.core.money import D
from tradebot.core.store import Store
from tradebot.data.hub import MarketData
from tradebot.execution.adapters.base import PermanentExchangeError, TransientExchangeError
from tradebot.execution.adapters.paper import PaperAdapter
from tradebot.execution.order_manager import OrderManager
from tradebot.portfolio.ledger import EntryPlan, Ledger
from tradebot.portfolio.sizing import size_entry
from tradebot.risk.killswitch import KillSwitch

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _market(book_qty: str = "0.5") -> MarketData:
    md = MarketData(["BTC/USDT"], "1m", ["15m"])
    md.on_trade(Trade("BTC/USDT", NOW, D("60000"), D("1")))
    md.on_book(make_book(qty_per_level=book_qty))
    return md


def _adapter(cfg, md, clock, seed: int = 3, cash: str = "1000") -> PaperAdapter:
    rules = {"BTC/USDT": MarketRules("BTC/USDT", D("0.1"), D("0.00001"), D("0.00001"), D("5"), D("0.001"), D("0.001"))}
    return PaperAdapter(md, rules, cfg.settings.paper.model_copy(update={"api_fail_rate": D("0")}), clock, starting_cash=D(cash), seed=seed)


class TestPaperAdapter:
    async def test_market_buy_pays_fee_and_impact(self, cfg):
        clock = SimClock(NOW)
        md = _market()
        adapter = _adapter(cfg, md, clock, cash="100000")
        fills: list[Fill] = []
        adapter.on_fill = fills.append
        order = await adapter.place_order(
            OrderRequest(symbol="BTC/USDT", side=Side.BUY, qty=D("0.75"), type=OrderType.MARKET, price=D("60000"), strategy="t", tag="1")
        )
        assert order.status is OrderStatus.FILLED
        assert order.avg_fill_price > D("60000")  # walked the ask side
        assert order.fee == order.avg_fill_price * order.qty * D("0.001")
        assert fills[0].slippage_bps > 0
        balances = await adapter.get_balances()
        assert balances["BTC"] == D("0.75")
        assert balances["USDT"] < D("100000")

    async def test_insufficient_cash_is_rejected(self, cfg):
        clock = SimClock(NOW)
        adapter = _adapter(cfg, _market(), clock, cash="100")
        with pytest.raises(PermanentExchangeError):
            await adapter.place_order(
                OrderRequest(symbol="BTC/USDT", side=Side.BUY, qty=D("0.01"), type=OrderType.MARKET, price=D("60000"), strategy="t")
            )

    async def test_min_notional_rejected(self, cfg):
        adapter = _adapter(cfg, _market(), SimClock(NOW))
        with pytest.raises(PermanentExchangeError):
            await adapter.place_order(
                OrderRequest(symbol="BTC/USDT", side=Side.BUY, qty=D("0.00001"), type=OrderType.MARKET, price=D("60000"), strategy="t")
            )

    async def test_idempotent_retry(self, cfg):
        adapter = _adapter(cfg, _market(), SimClock(NOW))
        request = OrderRequest(symbol="BTC/USDT", side=Side.BUY, qty=D("0.001"), type=OrderType.MARKET, price=D("60000"), strategy="t", tag="same")
        first = await adapter.place_order(request)
        second = await adapter.place_order(request)
        assert first is second
        assert len(adapter.fills) == 1

    async def test_limit_only_fills_when_price_trades_through(self, cfg):
        clock = SimClock(NOW)
        md = _market()
        adapter = _adapter(cfg, md, clock)
        # limit buy below the market: no fill yet
        order = await adapter.place_order(
            OrderRequest(symbol="BTC/USDT", side=Side.BUY, qty=D("0.001"), type=OrderType.LIMIT, price=D("59900"), strategy="t", tag="lim")
        )
        assert order.status is OrderStatus.SUBMITTED
        # a trade at the level (touching) is not enough...
        md.on_trade(Trade("BTC/USDT", NOW + timedelta(seconds=1), D("59900"), D("1")))
        adapter.on_market_update("BTC/USDT")
        assert order.status is OrderStatus.SUBMITTED
        # ...price must trade through it
        md.on_trade(Trade("BTC/USDT", NOW + timedelta(seconds=2), D("59899"), D("1")))
        adapter.on_market_update("BTC/USDT")
        assert order.status is OrderStatus.FILLED

    async def test_stop_market_triggers_and_fills(self, cfg):
        clock = SimClock(NOW)
        md = _market()
        adapter = _adapter(cfg, md, clock)
        stop = await adapter.place_order(
            OrderRequest(
                symbol="BTC/USDT", side=Side.SELL, qty=D("0.001"), type=OrderType.STOP_MARKET,
                stop_price=D("59000"), strategy="t", tag="stop",
            )
        )
        await adapter.place_order(
            OrderRequest(symbol="BTC/USDT", side=Side.BUY, qty=D("0.001"), type=OrderType.MARKET, price=D("60000"), strategy="t", tag="entry")
        )
        md.on_trade(Trade("BTC/USDT", NOW + timedelta(seconds=3), D("58900"), D("1")))
        md.on_book(make_book(mid="58900"))
        adapter.on_market_update("BTC/USDT")
        await adapter.drain_pending()
        assert stop.status is OrderStatus.FILLED
        assert stop.avg_fill_price < D("59000")  # slippage through the stop

    async def test_api_failures_are_injected(self, cfg):
        clock = SimClock(NOW)
        rules = {"BTC/USDT": MarketRules("BTC/USDT", D("0.1"), D("0.00001"), D("0.00001"), D("5"), D("0.001"), D("0.001"))}
        adapter = PaperAdapter(
            _market(), rules, cfg.settings.paper.model_copy(update={"api_fail_rate": D("1")}), clock
        )
        with pytest.raises(TransientExchangeError):
            await adapter.place_order(
                OrderRequest(symbol="BTC/USDT", side=Side.BUY, qty=D("0.001"), type=OrderType.MARKET, price=D("60000"), strategy="t")
            )


class TestOrderManager:
    def _manager(self, cfg, adapter, clock):
        store = Store()
        ks = KillSwitch(store, clock=clock, mode=cfg.mode)
        return OrderManager(cfg, adapter, store, ks, clock), ks, store

    @pytest.mark.parametrize("level", [KillLevel.L1, KillLevel.L2, KillLevel.L3])
    async def test_entry_refused_at_actual_order_boundary(self, cfg, level):
        clock = SimClock(NOW)
        adapter = _adapter(cfg, _market(), clock)
        manager, ks, store = self._manager(cfg, adapter, clock)
        ks.trigger(level, trigger="test", reason="halt")
        signal = Signal(id="blocked", strategy="S1", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
                        entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"))
        assert await manager.submit_entry(signal, D("0.001"), price=D("60000"), plan=object()) is None
        assert adapter.orders == {}
        assert manager.plans == {}
        refused = [e for e in store.events() if e["kind"] == "autonomy_refused"]
        assert len(refused) == 1
        assert refused[0]["payload"]["kill_level"] == level.value

    async def test_entry_retry_is_blocked_after_external_kill(self, cfg, tmp_path):
        clock = SimClock(NOW)
        adapter = _adapter(cfg, _market(), clock)
        store = Store(tmp_path / "state.db")
        other_store = Store(tmp_path / "state.db")
        ks = KillSwitch(store, clock=clock, mode=Mode.PAPER)
        other = KillSwitch(other_store, clock=clock, mode=Mode.PAPER)
        manager = OrderManager(cfg, adapter, store, ks, clock)
        calls = []

        async def fail_then_escalate(request):
            calls.append(request.client_order_id)
            other.trigger(KillLevel.L3, trigger="watchdog", reason="halt")
            raise TransientExchangeError("temporary failure")

        adapter.place_order = fail_then_escalate  # type: ignore[assignment]
        signal = Signal(id="race", strategy="S1", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
                        entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"))
        assert await manager.submit_entry(signal, D("0.001"), price=D("60000")) is None
        assert len(calls) == 1
        assert store.count_events("autonomy_refused") == 1
        store.close()
        other_store.close()

    @pytest.mark.parametrize("lookup_fails", [True, False])
    async def test_ambiguous_order_is_not_blindly_retried(self, cfg, lookup_fails):
        clock = SimClock(NOW)
        from dataclasses import replace

        cfg = replace(cfg, settings=cfg.settings.model_copy(update={"mode": Mode.LIVE}))
        adapter = _adapter(cfg, _market(), clock)
        # Simulate an adapter whose local lookup cannot prove venue absence.
        adapter.mode = Mode.LIVE
        manager, ks, store = self._manager(cfg, adapter, clock)
        calls = []

        async def unknown_submit(request):
            calls.append(request.client_order_id)
            raise TransientExchangeError("timeout")

        async def lookup(cid):
            if lookup_fails:
                raise TransientExchangeError("lookup unavailable")
            return None

        adapter.place_order = unknown_submit  # type: ignore[assignment]
        adapter.get_order = lookup  # type: ignore[assignment]
        signal = Signal(id="unknown", strategy="S1", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
                        entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"))
        assert await manager.submit_entry(signal, D("0.001"), price=D("60000")) is None
        assert len(calls) == 1
        assert ks.level is KillLevel.L1
        assert store.count_events("order_outcome_unknown") == 1

    async def test_unknown_exit_is_not_retried_and_pauses_entries(self, cfg):
        clock = SimClock(NOW)
        adapter = _adapter(cfg, _market(), clock)
        manager, ks, store = self._manager(cfg, adapter, clock)
        adapter.mode = Mode.LIVE
        calls = []

        async def unknown_submit(request):
            calls.append(request.client_order_id)
            raise TransientExchangeError("exit timeout")

        adapter.place_order = unknown_submit  # type: ignore[assignment]
        assert await manager.submit_exit("BTC/USDT", D("0.001"), "S1", "risk exit") is None
        assert len(calls) == 1
        assert ks.level is KillLevel.L1
        assert store.count_events("order_outcome_unknown") == 1

    async def test_blocked_entry_does_not_submit_if_audit_write_fails(self, cfg, monkeypatch):
        clock = SimClock(NOW)
        adapter = _adapter(cfg, _market(), clock)
        manager, ks, store = self._manager(cfg, adapter, clock)
        ks.trigger(KillLevel.L1, trigger="test", reason="pause")

        def fail_log(*args, **kwargs):
            raise OSError("disk unavailable")

        monkeypatch.setattr(store, "log_event", fail_log)
        signal = Signal(id="disk", strategy="S1", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
                        entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"))
        with pytest.raises(OSError, match="disk unavailable"):
            await manager.submit_entry(signal, D("0.001"), price=D("60000"))
        assert adapter.orders == {}

    async def test_simulator_retries_only_after_authoritative_absence(self, cfg):
        clock = SimClock(NOW)
        adapter = _adapter(cfg, _market(), clock)
        manager, _, store = self._manager(cfg, adapter, clock)
        original = adapter.place_order
        calls = []

        async def fail_once(request):
            calls.append(request.client_order_id)
            if len(calls) == 1:
                raise TransientExchangeError("before acceptance")
            return await original(request)

        adapter.place_order = fail_once  # type: ignore[assignment]
        signal = Signal(id="safe", strategy="S1", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
                        entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"))
        assert await manager.submit_entry(signal, D("0.001"), price=D("60000")) is not None
        assert len(calls) == 2
        assert store.count_events("order_outcome_unknown") == 0

    async def test_entry_blocked_if_kill_occurs_during_rate_limit_wait(self, cfg):
        clock = SimClock(NOW)
        adapter = _adapter(cfg, _market(), clock)
        manager, ks, store = self._manager(cfg, adapter, clock)

        class WaitingBucket:
            def try_acquire(self):
                return False

            def time_until(self):
                return 1.0

        manager.bucket = WaitingBucket()  # type: ignore[assignment]
        original_sleep = clock.sleep

        async def sleep_and_pause(seconds):
            ks.trigger(KillLevel.L1, trigger="test", reason="pause during wait")
            await original_sleep(seconds)

        clock.sleep = sleep_and_pause  # type: ignore[method-assign]
        signal = Signal(id="wait", strategy="S1", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
                        entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"))
        assert await manager.submit_entry(signal, D("0.001"), price=D("60000")) is None
        assert adapter.orders == {}
        assert store.count_events("autonomy_refused") == 1

    @pytest.mark.parametrize("level", [KillLevel.L1, KillLevel.L2, KillLevel.L3])
    async def test_kill_check_does_not_block_paper_exit(self, cfg, level):
        clock = SimClock(NOW)
        adapter = _adapter(cfg, _market(), clock)
        manager, ks, _ = self._manager(cfg, adapter, clock)
        await adapter.place_order(OrderRequest(symbol="BTC/USDT", side=Side.BUY, qty=D("0.001"),
                                               type=OrderType.MARKET, strategy="S1", tag="open"))
        ks.trigger(level, trigger="test", reason="halt")
        order = await manager.submit_exit("BTC/USDT", D("0.001"), "S1", "risk exit")
        assert order is not None
        assert order.status is OrderStatus.FILLED
        assert adapter.holdings["BTC"] == 0

    async def test_transient_error_is_retried_once_order_is_found(self, cfg):
        clock = SimClock(NOW)
        adapter = _adapter(cfg, _market(), clock)
        manager, _, store = self._manager(cfg, adapter, clock)
        calls = {"n": 0}
        original = adapter.place_order

        async def flaky(request):
            calls["n"] += 1
            if calls["n"] == 1:
                # place it at the venue but report a timeout to the caller
                await original(request)
                raise TransientExchangeError("timeout")
            return await original(request)

        adapter.place_order = flaky  # type: ignore[assignment]
        from tradebot.core.models import Signal

        signal = Signal(
            id="sig1", strategy="S1_ema_trend", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
            entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"),
        )
        order = await manager.submit_entry(signal, D("0.001"), price=D("60000"))
        assert order is not None
        # queried by client id instead of resending a duplicate
        assert len(adapter.orders) == 1
        assert len(adapter.fills) == 1

    async def test_reject_is_not_retried_and_counts_towards_kill_switch(self, cfg):
        clock = SimClock(NOW)
        adapter = _adapter(cfg, _market(), clock, cash="1")  # far too little cash
        manager, ks, store = self._manager(cfg, adapter, clock)
        from tradebot.core.models import Signal

        for i in range(cfg.risk.rejects_limit):
            signal = Signal(
                id=f"sig2-{i}", strategy="S1_ema_trend", symbol="BTC/USDT", timeframe="15m",
                side=Side.BUY, entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"),
            )
            await manager.submit_entry(signal, D("0.01"), price=D("60000"))
        assert manager.stats.rejected >= 1
        assert ks.level.value == "L1"  # reject storm escalates

    async def test_stale_entries_are_cancelled(self, cfg):
        clock = SimClock(NOW)
        md = _market()
        adapter = _adapter(cfg, md, clock)
        manager, _, _ = self._manager(cfg, adapter, clock)
        from tradebot.core.models import Signal

        signal = Signal(
            id="sig3", strategy="S1_ema_trend", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
            entry_type=OrderType.LIMIT, ts=NOW, stop=D("58000"), target=D("62000"), limit_price=D("59000"),
        )
        order = await manager.submit_entry(signal, D("0.001"), price=D("59000"))
        assert order is not None and order.status.is_open
        cancelled = await manager.expire_stale_entries(NOW + timedelta(seconds=cfg.settings.execution.entry_timeout_seconds + 1))
        assert cancelled == 1
        assert adapter.orders[order.client_order_id].status is OrderStatus.CANCELED


class TestLedger:
    def _fill(self, side: Side, price: str, qty: str, fee: str = "0", reason: str = "") -> Fill:
        return Fill(
            order_id="o", symbol="BTC/USDT", side=side, price=D(price), qty=D(qty),
            fee=D(fee), ts=NOW, strategy="S1_ema_trend", reason=reason,
        )

    def test_open_and_close_realises_pnl(self):
        ledger = Ledger(starting_cash=D("1000"))
        plan = EntryPlan(symbol="BTC/USDT", strategy="S1_ema_trend", stop=D("59000"), target=D("62000"))
        ledger.on_fill(self._fill(Side.BUY, "60000", "0.01", "0.6"), plan)
        pos = ledger.get("BTC/USDT", "S1_ema_trend")
        assert pos is not None and pos.qty == D("0.01")
        closed = ledger.on_fill(self._fill(Side.SELL, "61000", "0.01", "0.61", "target"))
        assert closed is not None
        assert closed.pnl == D("10") - D("0.61")
        assert ledger.get("BTC/USDT", "S1_ema_trend") is None
        assert ledger.cash == D("1000") - D("600") - D("0.6") + D("610") - D("0.61")

    def test_partial_exit_keeps_position(self):
        ledger = Ledger(starting_cash=D("1000"))
        plan = EntryPlan(symbol="BTC/USDT", strategy="S1_ema_trend", stop=D("59000"), target=D("62000"))
        ledger.on_fill(self._fill(Side.BUY, "60000", "0.01", "0.6"), plan)
        closed = ledger.on_fill(self._fill(Side.SELL, "60500", "0.005", "0.3", "partial"))
        assert closed is None
        pos = ledger.get("BTC/USDT", "S1_ema_trend")
        assert pos.qty == D("0.005")
        assert ledger.realized_pnl == D("2.5") - D("0.3")

    def test_invariant_and_reconciliation(self):
        ledger = Ledger(starting_cash=D("1000"))
        plan = EntryPlan(symbol="BTC/USDT", strategy="S1_ema_trend", stop=D("59000"), target=D("62000"))
        ledger.on_fill(self._fill(Side.BUY, "60000", "0.01", "0.6"), plan)
        marks = {"BTC/USDT": D("60500")}
        ok, detail = ledger.check_invariant(marks)
        assert ok, detail
        ok, _ = ledger.reconcile_with_adapter(ledger.cash)
        assert ok
        ok, detail = ledger.reconcile_with_adapter(ledger.cash + D("5"))
        assert not ok and "mismatch" in detail

    def test_exit_without_position_raises(self):
        ledger = Ledger(starting_cash=D("1000"))
        with pytest.raises(ValueError):
            ledger.on_fill(self._fill(Side.SELL, "60000", "0.01"))

    def test_tax_reserve_accrues_on_gains_only(self):
        ledger = Ledger(starting_cash=D("1000"))
        plan = EntryPlan(symbol="BTC/USDT", strategy="S1_ema_trend", stop=D("59000"), target=D("62000"))
        ledger.on_fill(self._fill(Side.BUY, "60000", "0.01"), plan)
        ledger.on_fill(self._fill(Side.SELL, "61000", "0.01", "0", "target"))
        accrued = ledger.accrue_tax_reserve(D("0.30"))
        assert accrued == pytest.approx(D("3"), abs=Decimal("0.01"))


class TestSizing:
    def _rules(self) -> MarketRules:
        return MarketRules("BTC/USDT", D("0.1"), D("0.00001"), D("0.00001"), D("5"), D("0.001"), D("0.001"))

    def test_risk_per_trade_binds(self, cfg):
        size = size_entry(
            price=D("60000"), stop=D("58000"), equity=D("1000"), starting_capital=D("1000"),
            limits=cfg.risk, rules=self._rules(),
        )
        # 0.5% of 1000 = 5 USDT of risk over a 2000 USDT stop distance = 0.0025 BTC,
        # and that lands exactly on the max-notional cap, so risk stays the binding rule.
        assert size.qty == D("0.0025")
        assert size.binding == "risk_per_trade"

    def test_max_notional_binds(self, cfg):
        size = size_entry(
            price=D("100"), stop=D("99"), equity=D("1000"), starting_capital=D("1000"),
            limits=cfg.risk, rules=self._rules(),
        )
        assert size.notional <= cfg.risk.max_notional
        assert size.binding in ("max_notional", "per_symbol_exposure", "total_exposure", "bucket_exposure")

    def test_book_depth_cap(self, cfg):
        book = make_book(qty_per_level="0.001")  # thin book: 20*0.001*60000 / 10 = 120 USDT of 10% depth
        size = size_entry(
            price=D("60000"), stop=D("59000"), equity=D("1000"), starting_capital=D("1000"),
            limits=cfg.risk, rules=self._rules(), book=book,
        )
        assert size.notional <= cfg.risk.max_order_book_pct * book.notional_depth(Side.BUY, 10) + D("1")

    def test_available_cash_cap(self, cfg):
        size = size_entry(
            price=D("60000"), stop=D("59999"), equity=D("1000"), starting_capital=D("1000"),
            limits=cfg.risk, rules=self._rules(), available_cash=D("50"),
        )
        assert size.notional <= D("50")
        assert size.binding == "available_cash"

    def test_below_min_notional_returns_zero(self, cfg):
        size = size_entry(
            price=D("60000"), stop=D("59000"), equity=D("10"), starting_capital=D("10"),
            limits=cfg.risk, rules=self._rules(),
        )
        assert size.qty == 0 and not size.ok


@pytest.mark.parametrize("mutation", ["mode", "risk", "adapter_mode", "adapter"])
async def test_order_boundary_refuses_runtime_risk_mode_or_adapter_changes(cfg, mutation):
    from dataclasses import replace

    clock = SimClock(NOW)
    adapter = _adapter(cfg, _market(), clock)
    store = Store(":memory:")
    switch = KillSwitch(store, clock=clock, kill_file=None, mode=Mode.PAPER)
    manager = OrderManager(cfg, adapter, store, switch, clock)
    if mutation == "mode":
        cfg.settings.mode = Mode.LIVE
    elif mutation == "risk":
        manager.cfg = replace(cfg, risk=cfg.risk.model_copy(update={"risk_per_trade": D("0.02")}))
    elif mutation == "adapter_mode":
        adapter.mode = Mode.LIVE
    else:
        manager.adapter = _adapter(cfg, _market(), clock)
    signal = Signal(id="immutable", strategy="S1", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
                    entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"))
    assert await manager.submit_entry(signal, D("0.001"), price=D("60000")) is None
    assert not adapter.orders
    assert store.count_events("autonomy_refused") == 1
    store.close()


async def test_entry_refused_when_initial_adapter_mode_does_not_match_config(cfg):
    clock = SimClock(NOW)
    adapter = _adapter(cfg, _market(), clock)
    adapter.mode = Mode.LIVE
    store = Store(":memory:")
    switch = KillSwitch(store, clock=clock, kill_file=None, mode=Mode.PAPER)
    manager = OrderManager(cfg, adapter, store, switch, clock)
    signal = Signal(id="wrong-mode", strategy="S1", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
                    entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"))
    assert await manager.submit_entry(signal, D("0.001"), price=D("60000")) is None
    assert not adapter.orders
    assert store.count_events("autonomy_refused") == 1
    store.close()


async def test_reduce_only_buy_cannot_bypass_kill_check(cfg):
    clock = SimClock(NOW)
    adapter = _adapter(cfg, _market(), clock)
    store = Store(":memory:")
    switch = KillSwitch(store, clock=clock, kill_file=None, mode=Mode.PAPER)
    manager = OrderManager(cfg, adapter, store, switch, clock)
    switch.trigger(KillLevel.L2, trigger="test", reason="stop")
    request = OrderRequest(symbol="BTC/USDT", side=Side.BUY, qty=D("0.001"),
                           type=OrderType.MARKET, reduce_only=True)
    assert await manager._place(request) is None
    assert not adapter.orders
    assert store.count_events("autonomy_refused") == 1
    store.close()


async def test_order_boundary_refuses_mismatched_kill_switch_mode(cfg):
    clock = SimClock(NOW)
    adapter = _adapter(cfg, _market(), clock)
    store = Store(":memory:")
    switch = KillSwitch(store, clock=clock, kill_file=None, mode=Mode.LIVE)
    manager = OrderManager(cfg, adapter, store, switch, clock)
    signal = Signal(id="wrong-switch", strategy="S1", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
                    entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"))
    assert await manager.submit_entry(signal, D("0.001"), price=D("60000")) is None
    assert not adapter.orders
    assert store.count_events("autonomy_refused") == 1
    store.close()


async def test_order_boundary_refuses_replaced_kill_switch(cfg):
    clock = SimClock(NOW)
    adapter = _adapter(cfg, _market(), clock)
    store, other = Store(":memory:"), Store(":memory:")
    manager = OrderManager(cfg, adapter, store, KillSwitch(store, clock=clock, kill_file=None, mode=Mode.PAPER), clock)
    manager.killswitch = KillSwitch(other, clock=clock, kill_file=None, mode=Mode.PAPER)
    signal = Signal(id="switch-swap", strategy="S1", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
                    entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"))
    assert await manager.submit_entry(signal, D("0.001"), price=D("60000")) is None
    assert not adapter.orders
    assert store.count_events("autonomy_refused") == 1
    store.close()
    other.close()


async def test_durable_flapping_hold_blocks_entry_before_l1(cfg):
    clock = SimClock(NOW)
    adapter = _adapter(cfg, _market(), clock)
    store = Store(":memory:")
    switch = KillSwitch(store, clock=clock, kill_file=None, mode=Mode.PAPER)
    manager = OrderManager(cfg, adapter, store, switch, clock)
    store.extend_flapping_hold(NOW + timedelta(minutes=10), ["feed"], ts=NOW)
    signal = Signal(id="hold-entry", strategy="S1", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
                    entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"))
    assert switch.level is KillLevel.L0
    assert await manager.submit_entry(signal, D("0.001"), price=D("60000")) is None
    assert not adapter.orders
    store.close()


async def test_disabled_strategy_cannot_submit_entry_but_exit_path_remains_available(cfg):
    clock = SimClock(NOW)
    adapter = _adapter(cfg, _market(), clock)
    store = Store(":memory:")
    manager = OrderManager(cfg, adapter, store, KillSwitch(store, clock=clock, kill_file=None, mode=Mode.PAPER), clock)
    store.disable_strategy("S1", "fault", faults=3, ts=NOW)
    signal = Signal(id="disabled-entry", strategy="S1", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
                    entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"))
    assert await manager.submit_entry(signal, D("0.001"), price=D("60000")) is None
    assert not adapter.orders
    assert store.count_events("autonomy_refused") == 1
    # A disabled strategy's exit must reach the adapter, even if paper rejects it for no position.
    calls = []
    original = adapter.place_order

    async def capture(request):
        calls.append(request)
        return await original(request)

    adapter.place_order = capture
    await manager.submit_exit("BTC/USDT", D("0.001"), "S1", "protect")
    assert len(calls) == 1 and calls[0].reduce_only
    store.close()


async def test_fault_pause_blocks_direct_entry_but_expires(cfg):
    clock = SimClock(NOW)
    adapter = _adapter(cfg, _market(), clock)
    store = Store(":memory:")
    manager = OrderManager(cfg, adapter, store, KillSwitch(store, clock=clock, kill_file=None, mode=Mode.PAPER), clock)
    store.record_strategy_fault("S1", "fault", now=NOW)
    signal = Signal(id="paused-entry", strategy="S1", symbol="BTC/USDT", timeframe="15m", side=Side.BUY,
                    entry_type=OrderType.MARKET, ts=NOW, stop=D("59000"), target=D("62000"))
    assert await manager.submit_entry(signal, D("0.001"), price=D("60000")) is None
    assert not adapter.orders
    assert store.count_events("autonomy_refused") == 1
    clock.advance(timedelta(hours=1, seconds=1))
    assert not store.strategy_fault_paused("S1", now=clock.now())
    store.close()
