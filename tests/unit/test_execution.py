"""Paper exchange realism, order manager retries, ledger invariants and sizing caps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.conftest import make_book

from tradebot.core.clock import SimClock
from tradebot.core.models import (
    Fill,
    MarketRules,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
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
        ks = KillSwitch(store, clock=clock)
        return OrderManager(cfg, adapter, store, ks, clock), ks, store

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
