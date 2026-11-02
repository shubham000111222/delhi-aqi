"""The bot: one asyncio process wiring data, strategies, risk, execution and reporting.

Everything the plan asks for in the hot path lives here and the same class runs in
backtest, paper and live - only the feed and the adapter are swapped out.

Event flow (all sequential, so behaviour is deterministic and replayable):

    feed --tick/book/bar--> queue --> engine
      bar close -> strategies (per symbol x timeframe) -> signals
                -> risk engine (16 checks + sizing) -> order manager -> adapter
      fill <-adapter-> queue -> ledger + position management (stops, targets, trail)
      health loop -> kill switch triggers, staleness, reconciliation, day boundary
"""

from __future__ import annotations

import asyncio
import shutil
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .config import Config, Mode
from .core.clock import Clock, SimClock, iso, utc, utcnow
from .core.logging import JsonMessage, get_logger
from .core.models import (
    Candle,
    EquitySnapshot,
    Fill,
    KillLevel,
    OrderBook,
    OrderRequest,
    OrderType,
    Position,
    Regime,
    Side,
    Signal,
    Trade,
)
from .core.money import ZERO, D, round_qty, to_str
from .core.store import Store
from .data.feed import BaseFeed, make_feed
from .data.hub import MarketData
from .execution.adapters.base import ExchangeAdapter
from .execution.order_manager import OrderManager
from .portfolio.ledger import ClosedTrade, EntryPlan, Ledger
from .reporting.daily import build_daily_report, tax_ledger_rows, write_reports
from .risk.engine import RiskContext, RiskEngine
from .risk.killswitch import KillSwitch
from .risk.live_guards import LiveGuardChecker, RampController
from .strategies.base import Strategy, StrategyContext
from .strategies.regime import atr_value, detect_regime

log = get_logger(__name__)


@dataclass
class EngineStats:
    events: int = 0
    ticks: int = 0
    books: int = 0
    bars: int = 0
    signals: int = 0
    vetoes: int = 0
    orders: int = 0
    fills: int = 0
    trades_closed: int = 0
    reconnects: int = 0
    gaps: int = 0
    started_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "ticks": self.ticks,
            "books": self.books,
            "bars": self.bars,
            "signals": self.signals,
            "vetoes": self.vetoes,
            "orders": self.orders,
            "fills": self.fills,
            "trades_closed": self.trades_closed,
            "reconnects": self.reconnects,
            "gaps": self.gaps,
            "started_at": iso(self.started_at),
        }


class Engine:
    def __init__(
        self,
        config: Config,
        store: Store,
        clock: Clock,
        market: MarketData,
        feed: BaseFeed,
        adapter: ExchangeAdapter,
        killswitch: KillSwitch,
        ledger: Ledger,
        risk: RiskEngine,
        order_manager: OrderManager,
        strategies: dict[str, Strategy],
        notifier: Any = None,
    ) -> None:
        self.cfg = config
        self.store = store
        self.clock = clock
        self.market = market
        self.feed = feed
        self.adapter = adapter
        self.killswitch = killswitch
        self.ledger = ledger
        self.risk = risk
        self.om = order_manager
        self.strategies = strategies
        self.notifier = notifier
        self.stats = EngineStats()
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1_000)
        self._deferred: deque[tuple[str, Any]] = deque()
        self.running = False
        self.halted = False
        self._exiting: dict[tuple[str, str], datetime] = {}
        self._cooldowns: dict[str, datetime] = {}
        self._last_regime: dict[tuple[str, str], Regime] = {}
        self._consecutive_losses: dict[str, int] = {}
        self._last_day: str = ""
        self._last_health = 0.0
        self._last_snapshot: datetime | None = None
        self._equity_points = 0
        self._reconcile_state = "ok"
        self.starting_capital = config.settings.starting_capital_usdt
        self.ramp = RampController(config, store)
        self._load_state()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    async def build(
        cls,
        config: Config,
        *,
        clock: Clock | None = None,
        store: Store | None = None,
        feed: BaseFeed | None = None,
        adapter: ExchangeAdapter | None = None,
        notifier: Any = None,
    ) -> Engine:
        config.ensure_dirs()
        clock = clock or Clock()
        store = store or Store(config.settings.db_path)
        market = MarketData(
            symbols=config.symbol_names(),
            base_timeframe=config.settings.base_timeframe,
            timeframes=list(config.settings.strategy_timeframes),
            spike_pct=config.settings.data_quality.spike_pct,
        )
        feed = feed or make_feed(config, clock)
        rules = feed.market_rules()
        if adapter is None:
            from .execution.adapters import make_adapter

            adapter = make_adapter(config, market, clock, rules)
            connect = getattr(adapter, "connect", None)
            if connect is not None:  # live / testnet: load markets and market rules
                await connect()
        killswitch = KillSwitch(store, clock=clock)
        risk = RiskEngine(config, killswitch)
        ledger = Ledger(
            starting_cash=config.settings.starting_capital_usdt,
            quote_currency=config.settings.quote_currency,
        )
        manager = OrderManager(config, adapter, store, killswitch, clock)
        from .strategies.registry import build_all

        strategies = build_all(config.strategies, only_enabled=True)
        engine = cls(
            config=config,
            store=store,
            clock=clock,
            market=market,
            feed=feed,
            adapter=adapter,
            killswitch=killswitch,
            ledger=ledger,
            risk=risk,
            order_manager=manager,
            strategies=strategies,
            notifier=notifier,
        )
        manager.on_fill_callback = engine.enqueue_fill
        engine._restore_positions()
        return engine

    def _restore_positions(self) -> None:
        """Crash recovery: adopt positions and orders that survived the restart."""
        positions = self.store.open_positions()
        for pos in positions:
            self.ledger.positions[(pos.symbol, pos.strategy)] = pos
        if positions:
            log.warning(
                '{"event":"positions_restored","count":%d,"symbols":"%s"}',
                len(positions),
                ",".join(sorted({p.symbol for p in positions})),
            )
        cash = self.store.last_equity()
        if cash is not None and not positions:
            # continue from the stored cash balance rather than resetting to capital
            self.ledger.cash = cash

    def _load_state(self) -> None:
        day_start = self.store.kv_get("day_start_equity")
        if day_start is None:
            self.store.kv_set("day_start_equity", to_str(self.starting_capital, 8))
        week = self.store.kv_get("week_start_equity")
        if week is None:
            self.store.kv_set("week_start_equity", to_str(self.starting_capital, 8))
        peak = self.store.kv_get("peak_equity")
        if peak is None:
            self.store.kv_set("peak_equity", to_str(self.starting_capital, 8))
        self._consecutive_losses = self.store.kv_get("consecutive_losses", {}) or {}
        self._last_day = str(self.store.kv_get("last_day", ""))

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    async def emit(self, kind: str, payload: Any) -> None:
        """Called by the feed. Awaiting here gives natural backpressure: the producer
        never runs ahead of the engine, and event order is exactly arrival order."""
        await self.queue.put((kind, payload))

    def enqueue_fill(self, fill: Fill) -> None:
        """Adapter fills arrive synchronously, possibly while the queue is full."""
        try:
            self.queue.put_nowait(("fill", fill))
        except asyncio.QueueFull:
            self._deferred.append(("fill", fill))

    @property
    def marks(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for symbol in self.cfg.symbol_names():
            price = self.market.price(symbol)
            if price is not None:
                out[symbol] = price
        return out

    def equity(self) -> Decimal:
        return self.ledger.equity(self.marks)

    async def dispatch(self, kind: str, payload: Any) -> None:
        self.stats.events += 1
        if self.stats.events % 200 == 0:
            self._heartbeat()
        if isinstance(self.clock, SimClock):
            # Replay/backtest time advances with the events we actually process, so
            # freshness checks, cooldowns and deadlines are measured on market time.
            ts = getattr(payload, "ts", None)
            if isinstance(ts, datetime):
                self.clock.set(ts)
        if kind == "tick":
            await self._on_tick(payload)
        elif kind == "book":
            await self._on_book(payload)
        elif kind == "bar":
            await self._on_bar(payload)
        elif kind == "fill":
            await self._on_fill(payload)
        elif kind == "regime":
            self.store.log_event("regime", payload, actor="market")
        else:  # pragma: no cover - defensive
            log.warning('{"event":"unknown_event","kind":"%s"}', kind)

    # ------------------------------------------------------------------
    # market data handlers
    # ------------------------------------------------------------------
    async def _on_tick(self, trade: Trade) -> None:
        self.stats.ticks += 1
        if not self.market.on_trade(trade):
            return  # quarantined by the spike filter
        await self._after_market_update(trade.symbol)

    async def _on_book(self, book: OrderBook) -> None:
        self.stats.books += 1
        self.market.on_book(book)
        await self._after_market_update(book.symbol)

    async def _after_market_update(self, symbol: str) -> None:
        hook = getattr(self.adapter, "on_market_update", None)
        if callable(hook):
            hook(symbol)
        drain = getattr(self.adapter, "drain_pending", None)
        if callable(drain):
            await drain()
        await self._manage_positions(symbol)

    async def _on_bar(self, candle: Candle) -> None:
        self.stats.bars += 1
        closed = self.market.on_base_bar(candle)
        if not closed:
            return
        # a bar is *received* at its close time, not at its open time
        self.market.mark_received(candle.symbol, max(candle.close_time, self.clock.now()))
        for bar in closed:
            if bar.timeframe != self.cfg.settings.base_timeframe:
                await self._run_strategies(bar)
                self._advance_hold(bar)
                await self._check_time_stops(bar)
            if self.stats.bars % 60 == 0:
                self.store.log_event(
                    "bar_closed",
                    {"symbol": bar.symbol, "timeframe": bar.timeframe, "ts": iso(bar.ts)},
                    actor="market",
                )
        await self._snapshot_equity()

    async def _run_strategies(self, bar: Candle) -> None:
        series = self.market.candles(bar.symbol, bar.timeframe)
        if len(series) == 0:
            return
        regime = detect_regime(series, self.cfg.strategies.regime)
        self._last_regime[(bar.symbol, bar.timeframe)] = regime
        mark = self.market.price(bar.symbol) or bar.close
        for name, strategy in self.strategies.items():
            if not strategy.allows_timeframe(bar.timeframe):
                continue
            if self._strategy_blocked(name):
                continue
            if self.ledger.get(bar.symbol, name) is not None:
                continue  # one position per symbol-strategy pair
            if self.om.has_pending_entry(bar.symbol, name):
                continue  # an entry is already working for this pair
            ctx = StrategyContext(
                symbol=bar.symbol,
                timeframe=bar.timeframe,
                series=series,
                ts=bar.ts,
                mark=mark,
                atr=atr_value(series, self.cfg.strategies.regime.adx_period),
                regime=regime,
                equity=self.equity(),
                book=self.market.book(bar.symbol),
                position=None,
            )
            try:
                signal = strategy.evaluate(ctx)
            except Exception as exc:  # noqa: BLE001 - a strategy bug must not stop the bot
                self.store.log_event(
                    "strategy_error",
                    {"strategy": name, "symbol": bar.symbol, "error": str(exc)},
                    level="CRIT",
                    actor="strategy",
                )
                self._pause_strategy(name, hours=1, reason=f"exception: {exc}")
                continue
            if signal is None:
                continue
            self.stats.signals += 1
            self.store.save_signal(signal)
            self.store.log_event(
                "signal",
                {"signal_id": signal.id, "strategy": name, "symbol": signal.symbol, "side": signal.side.value},
                actor="strategy",
            )
            await self._try_enter(signal, ctx)

    # ------------------------------------------------------------------
    # entry path
    # ------------------------------------------------------------------
    async def _try_enter(self, signal: Signal, ctx: StrategyContext) -> None:
        if not self.killswitch.can_open_entries():
            self.stats.vetoes += 1
            self.store.save_verdict(signal.id, self._veto("kill_switch", f"kill level {self.killswitch.level.value}"))
            return
        rules = self.adapter.get_market_rules(signal.symbol)
        risk_ctx = self._risk_context(signal.symbol)
        decision = self.risk.approve_entry(signal, risk_ctx, rules)
        self.store.save_verdict(signal.id, decision.verdict, ts=self.clock.now())
        if not decision.approved:
            self.stats.vetoes += 1
            self.store.log_event(
                "risk_veto",
                {
                    "signal_id": signal.id,
                    "strategy": signal.strategy,
                    "symbol": signal.symbol,
                    "reason": decision.verdict.reason,
                },
                actor="risk",
            )
            return
        plan = EntryPlan(
            symbol=signal.symbol,
            strategy=signal.strategy,
            stop=signal.stop,
            target=signal.target,
            signal_id=signal.id,
            atr=D(ctx.atr) if ctx.atr == ctx.atr else ZERO,
        )
        await self.om.submit_entry(signal, decision.qty, price=decision.entry_price, plan=plan)
        self.stats.orders += 1

    def _veto(self, name: str, detail: str):
        from .core.models import RiskCheck, RiskVerdict

        return RiskVerdict(approved=False, reason=f"{name}: {detail}", checks=[RiskCheck(name, False, detail)])

    def _risk_context(self, symbol: str) -> RiskContext:
        marks = self.marks
        equity = self.ledger.equity(marks)
        book = self.market.book(symbol)
        now = self.clock.now()
        positions = self.ledger.open_positions
        bucket = self.cfg.settings.portfolio.bucket_of(symbol)
        bucket_symbols = self.cfg.settings.portfolio.correlated_buckets.get(bucket, [symbol])
        week_start = D(str(self.store.kv_get("week_start_equity", to_str(self.starting_capital))))
        day_start = D(str(self.store.kv_get("day_start_equity", to_str(equity))))
        peak = max(D(str(self.store.kv_get("peak_equity", to_str(equity)))), equity)
        return RiskContext(
            now=now,
            equity=equity,
            starting_capital=self.starting_capital,
            day_start_equity=day_start,
            week_start_equity=week_start,
            peak_equity=peak,
            symbol_exposure=self.ledger.symbol_exposure(symbol, marks),
            total_exposure=self.ledger.exposure(marks),
            bucket_exposure=self.ledger.bucket_exposure(bucket_symbols, marks),
            open_positions=len(positions) + len(self.om.pending),
            orders_last_minute=self.om.orders_last_minute,
            orders_today=self.om.orders_today,
            data_age_seconds=self.market.age_seconds(symbol, now),
            mid=self.market.price(symbol),
            spread_pct=book.spread_pct if book else ZERO,
            book=book,
            cooldown_until=self._cooldowns.get(symbol),
            symbol_enabled=symbol in self.cfg.symbol_names(),
            strategy_paused=False,
            mode_allows_entries=self.killswitch.level is KillLevel.L0,
            ramp_multiplier=self.ramp.multiplier(),
            available_cash=self.adapter_cash(),
        )

    def adapter_cash(self) -> Decimal:
        cash = getattr(self.adapter, "cash", None)
        return D(str(cash)) if cash is not None else self.ledger.cash

    # ------------------------------------------------------------------
    # fills and position lifecycle
    # ------------------------------------------------------------------
    async def _on_fill(self, fill: Fill) -> None:
        self.stats.fills += 1
        key = (fill.symbol, fill.strategy)
        pos_before = self.ledger.get(fill.symbol, fill.strategy)
        qty_before = pos_before.qty if pos_before else ZERO
        order = self.om.adapter_orders().get(fill.order_id)
        if order is not None:
            self.store.upsert_order(order)

        if fill.side is Side.BUY:
            plan = self.om.plan_for(fill.order_id)
            try:
                self.ledger.on_fill(fill, plan)
            except ValueError as exc:
                self.store.log_event(
                    "ledger_error", {"error": str(exc), "fill": fill.to_dict()}, level="CRIT", actor="ledger"
                )
                return
            pos = self.ledger.get(fill.symbol, fill.strategy)
            if pos is not None:
                self.store.save_position(pos)
                self.store.log_event(
                    "position_opened",
                    pos.to_dict(fill.price) | {"order_id": fill.order_id},
                    actor="portfolio",
                )
                await self._ensure_protection(pos)
        else:
            try:
                closed = self.ledger.on_fill(fill)
            except ValueError as exc:
                self.store.log_event(
                    "ledger_error", {"error": str(exc), "fill": fill.to_dict()}, level="CRIT", actor="ledger"
                )
                self._exiting.pop(key, None)
                return
            remaining = self.ledger.get(fill.symbol, fill.strategy)
            if remaining is not None:
                if qty_before > 0 and remaining.qty < qty_before:
                    remaining.partial_taken = True
                    if self.cfg.strategies.exit_management.breakeven_after_partial:
                        remaining.stop = max(remaining.stop, remaining.avg_entry)
                self.store.save_position(remaining)
                self.store.log_event(
                    "position_reduced",
                    remaining.to_dict(fill.price) | {"sold": to_str(fill.qty, 8), "reason": fill.reason},
                    actor="portfolio",
                )
            if pos_before is not None and closed is not None:
                self.store.close_position(pos_before, fill.price, closed.reason or "exit", closed_at=fill.ts)
                await self._on_trade_closed(closed)
            self._exiting.pop(key, None)
        self.om.record_fill(fill)

    async def _on_trade_closed(self, trade: ClosedTrade) -> None:
        self.stats.trades_closed += 1
        self.store.log_event("trade_closed", trade.to_dict(), actor="portfolio")
        if trade.pnl < 0 and trade.reason in ("stop", "stop_gap", "flatten"):
            self._cooldowns[trade.symbol] = self.clock.now() + timedelta(
                minutes=self.cfg.risk.cooldown_minutes_after_stop
            )
            self.store.log_event(
                "cooldown",
                {
                    "symbol": trade.symbol,
                    "until": iso(self._cooldowns[trade.symbol]),
                    "minutes": self.cfg.risk.cooldown_minutes_after_stop,
                },
                actor="risk",
            )
        streak = self._consecutive_losses.get(trade.strategy, 0)
        streak = streak + 1 if trade.pnl < 0 else 0
        self._consecutive_losses[trade.strategy] = streak
        self.store.kv_set("consecutive_losses", self._consecutive_losses)
        if trade.pnl < 0:
            if streak >= self.cfg.risk.consecutive_losses_pause:
                self._pause_strategy(
                    trade.strategy,
                    hours=self.cfg.risk.consecutive_losses_pause_hours,
                    reason=f"{streak} consecutive losses",
                )
            elif streak >= self.cfg.risk.consecutive_losses_halve:
                self.store.kv_set(f"size_mult_{trade.strategy}", to_str(D("0.5")))
                self.store.log_event(
                    "size_reduction",
                    {"strategy": trade.strategy, "multiplier": "0.5", "streak": streak},
                    level="WARN",
                    actor="risk",
                )
        else:
            self.store.kv_delete(f"size_mult_{trade.strategy}")
        self._reserve_tax()

    def _reserve_tax(self) -> None:
        pct = self.cfg.risk.tax_reserve_pct
        delta = self.ledger.accrue_tax_reserve(pct)
        if delta > 0:
            self.store.kv_set("tax_reserve", to_str(self.ledger.tax_reserve, 8))
            self.store.log_event(
                "tax_reserve",
                {"accrued": to_str(delta, 8), "total": to_str(self.ledger.tax_reserve, 8), "pct": str(pct)},
                actor="reporting",
            )

    async def _ensure_protection(self, pos: Position) -> None:
        """In live/testnet the stop must also sit on the exchange. Paper simulates it."""
        if self.adapter.mode is Mode.PAPER:
            return
        try:
            request = OrderRequest(
                symbol=pos.symbol,
                side=Side.SELL,
                qty=pos.qty,
                type=OrderType.STOP_MARKET,
                stop_price=pos.stop,
                strategy=pos.strategy,
                reduce_only=True,
                reason="protective stop",
            )
            order = await self.adapter.place_order(request)
            pos.stop_order_id = order.client_order_id
            self.store.upsert_order(order)
            self.store.save_position(pos)
        except Exception as exc:  # noqa: BLE001
            self.store.log_event(
                "protection_failed",
                {"symbol": pos.symbol, "strategy": pos.strategy, "error": str(exc)},
                level="CRIT",
                actor="execution",
            )
            await self._exit_position(pos, "protection_failed")

    async def _manage_positions(self, symbol: str) -> None:
        price = self.market.price(symbol)
        if price is None:
            return
        for pos in self.ledger.positions_for(symbol):
            key = (pos.symbol, pos.strategy)
            in_flight = self._exiting.get(key)
            if in_flight is not None and in_flight > self.clock.now():
                continue
            self._exiting.pop(key, None)
            pos.highest_price = max(pos.highest_price, price)

            # trailing stop never moves down
            series = self._strategy_series(pos)
            atr = atr_value(series, self.cfg.strategies.regime.adx_period) if series else 0.0
            if atr == atr and atr > 0:  # not NaN
                trail = pos.highest_price - D(atr * self.cfg.strategies.exit_management.trail_atr_mult)
                if pos.trailing_stop is None or trail > pos.trailing_stop:
                    pos.trailing_stop = max(trail, pos.effective_stop)

            stop = pos.effective_stop
            if price <= stop:
                await self._exit_position(pos, "stop", price=price)
                continue
            if price >= pos.target:
                await self._exit_position(pos, "target", price=price)
                continue
            if not pos.partial_taken and pos.risk_per_unit > 0:
                tp = pos.avg_entry + pos.risk_per_unit * self.cfg.risk.partial_tp_r
                if price >= tp:
                    await self._exit_position(
                        pos, "partial", fraction=self.cfg.risk.partial_tp_fraction, price=price
                    )

    def _strategy_series(self, pos: Position):
        tf = self.cfg.settings.strategy_timeframes[0]
        for candidate in self.cfg.settings.strategy_timeframes:
            if (pos.symbol, candidate) in self.market.series:
                tf = candidate
                if pos.strategy in self.cfg.enabled_strategies:
                    tfs = self.cfg.enabled_strategies[pos.strategy].timeframes
                    if candidate in tfs:
                        break
        series = self.market.series.get((pos.symbol, tf))
        return series if series is not None and len(series) > 20 else None

    async def _exit_position(
        self,
        pos: Position,
        reason: str,
        *,
        fraction: Decimal = Decimal("1"),
        price: Decimal | None = None,
    ) -> None:
        key = (pos.symbol, pos.strategy)
        now = self.clock.now()
        if self._exiting.get(key, now) > now:
            return
        self._exiting[key] = now + timedelta(seconds=120)
        qty = pos.qty if fraction >= 1 else round_qty(
            pos.qty * fraction, self.adapter.get_market_rules(pos.symbol).lot_step
        )
        if qty <= 0:
            self._exiting.pop(key, None)
            return
        if pos.stop_order_id:
            await self.adapter.cancel_order(pos.stop_order_id, pos.symbol)
            pos.stop_order_id = None
        label = reason if fraction >= 1 else f"partial ({reason})"
        order = await self.om.submit_exit(
            symbol=pos.symbol,
            qty=qty,
            strategy=pos.strategy,
            reason=label,
            order_type=OrderType.MARKET,
        )
        self.stats.orders += 1
        if order is None:
            self._exiting.pop(key, None)
            return
        self.store.log_event(
            "exit_submitted",
            {
                "symbol": pos.symbol,
                "strategy": pos.strategy,
                "qty": to_str(qty, 8),
                "reason": label,
                "reference_price": to_str(price or self.market.price(pos.symbol) or ZERO, 8),
            },
            actor="execution",
        )

    def _advance_hold(self, bar: Candle) -> None:
        for pos in self.ledger.positions_for(bar.symbol):
            tfs = self.cfg.enabled_strategies.get(pos.strategy)
            timeframes = tfs.timeframes if tfs else self.cfg.settings.strategy_timeframes
            if bar.timeframe in timeframes:
                pos.bars_held += 1

    async def _check_time_stops(self, bar: Candle) -> None:
        limit = self.cfg.risk.time_stop_bars
        for pos in self.ledger.positions_for(bar.symbol):
            tfs = self.cfg.enabled_strategies.get(pos.strategy)
            timeframes = tfs.timeframes if tfs else self.cfg.settings.strategy_timeframes
            if bar.timeframe in timeframes and pos.bars_held >= limit:
                await self._exit_position(pos, "time_stop")

    # ------------------------------------------------------------------
    # strategy pausing / decay
    # ------------------------------------------------------------------
    def _strategy_blocked(self, name: str) -> bool:
        paused = self.store.kv_get("paused_strategies", {}) or {}
        info = paused.get(name)
        if not info:
            return False
        until = info.get("until") if isinstance(info, dict) else None
        if until is None:
            return True
        return self.clock.now() < _parse(until)

    def _pause_strategy(self, name: str, *, hours: int, reason: str) -> None:
        paused = self.store.kv_get("paused_strategies", {}) or {}
        paused[name] = {"until": iso(self.clock.now() + timedelta(hours=hours)), "reason": reason}
        self.store.kv_set("paused_strategies", paused)
        self.store.log_event(
            "strategy_paused", {"strategy": name, "hours": hours, "reason": reason}, level="WARN", actor="risk"
        )

    # ------------------------------------------------------------------
    # equity, health, kill switch
    # ------------------------------------------------------------------
    async def _snapshot_equity(self) -> None:
        marks = self.marks
        if not marks:
            return
        snap = EquitySnapshot(
            ts=self.clock.now(),
            equity=self.ledger.equity(marks),
            cash=self.ledger.cash,
            positions_value=self.ledger.positions_value(marks),
            realized_pnl=self.ledger.realized_pnl,
            unrealized_pnl=self.ledger.unrealized(marks),
            fees=self.ledger.fees_paid,
        )
        self.store.add_equity(snap)
        self._last_snapshot = snap.ts
        peak = D(str(self.store.kv_get("peak_equity", to_str(snap.equity))))
        if snap.equity > peak:
            self.store.kv_set("peak_equity", to_str(snap.equity, 8))
        self._check_account_limits(snap.equity)
        await self._check_day_boundary(snap.equity)

    def _check_account_limits(self, equity: Decimal) -> None:
        ctx = RiskContext(
            now=self.clock.now(),
            equity=equity,
            starting_capital=self.starting_capital,
            day_start_equity=D(str(self.store.kv_get("day_start_equity", to_str(equity)))),
            week_start_equity=D(str(self.store.kv_get("week_start_equity", to_str(equity)))),
            peak_equity=D(str(self.store.kv_get("peak_equity", to_str(equity)))),
        )
        for reason, level in self.risk.account_alerts(ctx):
            if self.killswitch.level.rank < level.rank:
                self.killswitch.trigger(level, trigger="account_limit", reason=reason)
                if level.rank >= KillLevel.L2.rank:
                    asyncio.create_task(self.flatten_all(reason))

    def _heartbeat(self) -> None:
        """The watchdog watches this. Silence means the bot is wedged or dead."""
        self.store.kv_set("heartbeat", iso(self.clock.now()))
        self.store.kv_set("heartbeat_events", self.stats.events)

    async def health_check(self) -> dict[str, Any]:
        now = self.clock.now()
        self._heartbeat()
        dq = self.cfg.settings.data_quality
        stale = self.market.stale_symbols(now, dq.stale_symbol_seconds)
        all_stale = self.market.all_stale(now, dq.stale_all_seconds)
        disk = shutil.disk_usage(str(self.cfg.settings.data_dir))
        disk_used = disk.used / disk.total if disk.total else 0.0
        errors = self.store.count_events("order_error", since=now - timedelta(seconds=300))
        submitted = max(1, self.store.count_events("order_submitted", since=now - timedelta(seconds=300)))
        error_rate = errors / submitted

        if all_stale and self.stats.ticks > 0:
            self.killswitch.trigger(
                KillLevel.L1, trigger="stale_feeds", reason=f"all feeds stale > {dq.stale_all_seconds}s"
            )
        if disk_used >= self.cfg.risk.disk_pause_pct:
            self.killswitch.trigger(KillLevel.L1, trigger="disk_full", reason=f"disk {disk_used:.0%} used")
        elif disk_used >= self.cfg.risk.disk_warn_pct:
            self.store.log_event("disk_warning", {"used_pct": round(disk_used, 3)}, level="WARN", actor="health")
        if error_rate >= float(self.cfg.risk.exchange_error_rate_limit):
            self.killswitch.trigger(
                KillLevel.L1,
                trigger="exchange_error_rate",
                reason=f"{error_rate:.0%} order errors over 5 min",
            )
        self._reconcile_state = await self.reconcile()
        if self._reconcile_state != "ok":
            self.killswitch.trigger(
                KillLevel.L1, trigger="reconciliation", reason=str(self._reconcile_state)
            )
        await self.om.expire_stale_entries(now)
        self.killswitch.check_kill_file()
        if self.killswitch.must_flatten() and (self.ledger.open_positions or self.om.pending):
            await self.flatten_all(f"kill switch {self.killswitch.level.value}")
        health = {
            "ts": iso(now),
            "stale_symbols": stale,
            "all_stale": all_stale,
            "disk_used_pct": round(disk_used, 3),
            "error_rate": round(error_rate, 3),
            "reconciliation": self._reconcile_state,
            "kill_level": self.killswitch.level.value,
            "equity": to_str(self.equity(), 8),
            "open_positions": len(self.ledger.open_positions),
            "pending_orders": len(self.om.pending),
        }
        return health

    async def reconcile(self) -> str:
        """Compare the ledger with the venue. Any mismatch is an incident."""
        try:
            balances = await self.adapter.get_balances()
        except Exception as exc:  # noqa: BLE001
            return f"balance query failed: {exc}"
        adapter_cash = balances.get(self.cfg.settings.quote_currency)
        if adapter_cash is not None:
            ok, detail = self.ledger.reconcile_with_adapter(D(str(adapter_cash)))
            if not ok:
                self.store.add_incident("L1", "reconciliation", detail, {"balances": {k: str(v) for k, v in balances.items()}})
                return detail
        # Every open order must be known locally. The one exception is a resting
        # protective stop, which lives on the venue by design.
        try:
            open_orders = await self.adapter.get_open_orders()
        except Exception as exc:  # noqa: BLE001
            return f"open order query failed: {exc}"
        known = {o.client_order_id for o in self.om.pending.values()}
        unknown = [
            o
            for o in open_orders
            if o.status.is_open
            and o.client_order_id not in known
            and not (o.reduce_only and o.type is OrderType.LIMIT)
        ]
        if unknown:
            detail = "unknown open orders: " + ", ".join(f"{o.symbol}:{o.client_order_id}" for o in unknown)
            self.store.add_incident("L1", "reconciliation", detail, {"orders": [o.symbol for o in unknown]})
            return detail
        return "ok"

    async def flatten_all(self, reason: str) -> int:
        """L2/L3: cancel everything and close every position at market."""
        self.store.log_event("flatten_start", {"reason": reason}, level="CRIT", actor="killswitch")
        cancelled = await self.adapter.cancel_all()
        closed = 0
        for pos in list(self.ledger.open_positions):
            await self._exit_position(pos, "flatten", price=self.market.price(pos.symbol))
            closed += 1
        self.store.log_event(
            "flatten_done", {"reason": reason, "orders_cancelled": cancelled, "positions_exited": closed},
            level="CRIT", actor="killswitch",
        )
        return closed

    # ------------------------------------------------------------------
    # day boundary and reporting
    # ------------------------------------------------------------------
    async def _check_day_boundary(self, equity: Decimal) -> None:
        now = self.clock.now()
        day = utc(now).date().isoformat()
        if not self._last_day:
            self._last_day = day
            self.store.kv_set("last_day", day)
            return
        if day == self._last_day:
            return
        await self.emit_daily_report(day_end=now)
        self._last_day = day
        self.store.kv_set("last_day", day)
        self.store.kv_set("day_start_equity", to_str(equity, 8))
        self.om.reset_windows()
        if utc(now).weekday() == 0:
            self.store.kv_set("week_start_equity", to_str(equity, 8))

    async def emit_daily_report(
        self, day_end: datetime | None = None, day_start: datetime | None = None
    ) -> dict[str, Any]:
        now = day_end or self.clock.now()
        start = day_start or (utc(now).replace(hour=0, minute=0, second=0, microsecond=0))
        marks = self.marks
        equity = self.ledger.equity(marks)
        start_equity = D(str(self.store.kv_get("day_start_equity", to_str(equity))))
        report = build_daily_report(
            self.store,
            self.cfg,
            day_start=start,
            day_end=now,
            start_equity=start_equity,
            end_equity=equity,
            ledger_cash=self.ledger.cash,
            realized=self.ledger.realized_pnl,
            unrealized=self.ledger.unrealized(marks),
            fees=self.ledger.fees_paid,
            tax_reserve=self.ledger.tax_reserve,
            tds=self.ledger.tds_paid,
            peak_equity=D(str(self.store.kv_get("peak_equity", to_str(equity)))),
            marks=marks,
            open_positions=self.ledger.open_positions,
            reconciliation=str(self._reconcile_state),
            feed_gaps=self.stats.gaps,
            reconnects=self.stats.reconnects,
        )
        self._attach_ai_notes(report)
        paths = write_reports(
            report, self.cfg.settings.reports_dir, tax_ledger=tax_ledger_rows(report.closed_trades)
        )
        self.store.log_event(
            "daily_report",
            {"date": report.date, "net_pnl": to_str(report.net_pnl, 8), "paths": paths},
            actor="reporting",
        )
        if self.notifier is not None:
            try:
                await self.notifier.send(report.telegram(), level="INFO")
            except Exception as exc:  # noqa: BLE001
                self.store.log_event("alert_failed", {"error": str(exc)}, level="WARN", actor="alerts")
        return {"report": report.to_dict(), "paths": paths, "telegram": report.telegram()}

    def _attach_ai_notes(self, report) -> None:
        try:
            from .ai.journal import JournalAgent

            agent = JournalAgent(self.cfg)
            notes = agent.commentary(report)
            if notes:
                report.ai_notes = notes
                report.ai_source = "llm" if agent.used_llm else "template"
        except Exception:  # noqa: BLE001 - commentary is never allowed to break reporting
            report.ai_notes = []

    # ------------------------------------------------------------------
    # run loop
    # ------------------------------------------------------------------
    async def run(
        self,
        *,
        max_events: int | None = None,
        stop_at: datetime | None = None,
        idle_timeout: float | None = None,
    ) -> EngineStats:
        self.running = True
        self.store.log_event(
            "engine_start",
            {"mode": self.cfg.mode.value, "symbols": self.cfg.symbol_names(), "feed": type(self.feed).__name__},
            actor="engine",
        )
        feed_task = asyncio.create_task(self.feed.run(self.emit), name="feed")
        health_task = asyncio.create_task(self._health_loop(), name="health")
        processed = 0
        try:
            while self.running:
                if self._deferred:
                    kind, payload = self._deferred.popleft()
                    await self.dispatch(kind, payload)
                    processed += 1
                    continue
                try:
                    kind, payload = await asyncio.wait_for(
                        self.queue.get(), timeout=idle_timeout or 0.5
                    )
                except TimeoutError:
                    if max_events is not None and processed >= max_events:
                        break
                    if stop_at is not None and self.clock.now() >= stop_at:
                        break
                    if feed_task.done() and self.queue.empty():
                        break
                    continue
                await self.dispatch(kind, payload)
                processed += 1
                if max_events is not None and processed >= max_events:
                    break
                if stop_at is not None and self.clock.now() >= stop_at:
                    break
                if self.killswitch.level is KillLevel.L3:
                    break
        finally:
            self.running = False
            for task in (feed_task, health_task):
                task.cancel()
            await asyncio.gather(feed_task, health_task, return_exceptions=True)
            close = getattr(self.feed, "stop", None)
            if callable(close):
                close()
        self.store.log_event("engine_stop", self.stats.to_dict(), actor="engine")
        return self.stats

    async def _health_loop(self) -> None:
        interval = max(0.2, self.cfg.settings.execution.health_check_seconds)
        while self.running:
            await asyncio.sleep(interval)
            if isinstance(self.clock, SimClock):
                # in replay/backtest the loop is driven by bars, not wall time
                continue
            try:
                await self.health_check()
            except Exception:  # noqa: BLE001
                log.exception('{"event":"health_check_failed"}')

    # ------------------------------------------------------------------
    # controls (dashboard / CLI / telegram all call these)
    # ------------------------------------------------------------------
    async def pause(self, reason: str = "manual pause", actor: str = "operator") -> None:
        self.killswitch.trigger(KillLevel.L1, trigger="manual", reason=reason, actor=actor)

    async def kill(self, reason: str = "manual kill", actor: str = "operator") -> None:
        self.killswitch.trigger(KillLevel.L2, trigger="manual_kill", reason=reason, actor=actor)
        await self.flatten_all(reason)

    async def resume(self, reason: str = "manual resume", actor: str = "operator") -> None:
        self.killswitch.reset(reason=reason, actor=actor)

    def status(self) -> dict[str, Any]:
        marks = self.marks
        return {
            "mode": self.cfg.mode.value,
            "kill_level": self.killswitch.level.value,
            "kill_state": self.killswitch.state,
            "equity": to_str(self.ledger.equity(marks), 8),
            "cash": to_str(self.ledger.cash, 8),
            "realized_pnl": to_str(self.ledger.realized_pnl, 8),
            "unrealized_pnl": to_str(self.ledger.unrealized(marks), 8),
            "fees": to_str(self.ledger.fees_paid, 8),
            "tax_reserve": to_str(self.ledger.tax_reserve, 8),
            "open_positions": [p.to_dict(marks.get(p.symbol)) for p in self.ledger.open_positions],
            "pending_orders": len(self.om.pending),
            "stats": self.stats.to_dict(),
            "order_stats": self.om.stats.to_dict(),
            "ramp": self.ramp.load().to_dict(),
            "now": iso(self.clock.now()),
        }

    def close(self) -> None:
        self.store.close()


def _parse(text: str) -> datetime:
    return utc(datetime.fromisoformat(str(text).replace("Z", "+00:00")))


__all__ = ["Engine", "EngineStats", "JsonMessage", "LiveGuardChecker", "KillLevel"]
