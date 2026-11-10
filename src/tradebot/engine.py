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
import contextlib
import shutil
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .autonomy import AutonomyLedger, AutonomyLevel
from .config import Config, Mode
from .core.clock import Clock, SimClock, iso, parse_iso, timeframe_seconds, utc, utcnow
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
from .execution.slippage import SlippageRecorder, size_for_budget
from .heal import Playbook, Row
from .health import Check, HealthModel, RestartBudget, State
from .portfolio.ledger import ClosedTrade, EntryPlan, Ledger
from .reporting.daily import build_daily_report, tax_ledger_rows, write_reports
from .risk.engine import RiskContext, RiskEngine
from .risk.killswitch import KillSwitch
from .risk.live_guards import LiveGuardChecker, RampController
from .strategies.base import Strategy, StrategyContext
from .strategies.regime import atr_value, detect_regime

log = get_logger(__name__)
def _finite_decimal(value: Any) -> bool:
    """True when a Decimal (or something Decimal can take) is a real, finite number."""
    try:
        return D(value).is_finite()
    except (ArithmeticError, TypeError, ValueError):
        return False


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
        # §20.2: every component answers liveness / readiness / correctness, and the
        # model remembers only what changed. It holds no policy - the playbook does.
        self.health = HealthModel()
        for component in ("feed", "candles", "strategies", "orders", "store", "adapter"):
            self.health.register(component)
        # optional: an unconfigured notifier or model must not hold the system
        self.health.register("alerting", critical=False)
        self.health.register("ai", critical=False)
        self._restore_health_transitions()
        # §20.4: a deterministic bug restarts cleanly forever; three an hour, then stop.
        self.strategy_faults = RestartBudget(max_per_hour=3)
        self._disabled_strategies: dict[str, str] = {}
        self._logged_changes = 0
        # when an exit was submitted, so a stuck exit is a fact rather than an inference
        self._exit_attempts: dict[tuple[str, str], datetime] = {}
        # how many times the playbook has re-submitted an exit for a position that will
        # not close: two are a retry, the third is an escalation, never a loop
        self._exit_resubmits: dict[tuple[str, str], int] = {}
        # §20.3: the playbook is built once and ticked from the health loop
        self.playbook = self._build_playbook()
        # measured, not assumed: every fill with an expected price is recorded here
        self.slippage = SlippageRecorder()
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
        self._last_health_check: datetime | None = None
        # when the L1 pause condition was first observed as cleared (sustained health)
        self._healthy_since: datetime | None = None
        self._healthy_pause_state: dict[str, Any] | None = None
        # Venue seams (§18.8). Defaults describe a crypto venue: always open, no square-off.
        from .venues.calendar import AlwaysOpenCalendar
        from .venues.squareoff import SquareOffScheduler

        self.calendar: Any = AlwaysOpenCalendar()
        self.squareoff: Any = SquareOffScheduler(self.calendar)
        # Sentiment overlay (§19.4): a *reduce-only* size modifier. None means "no news
        # feed configured", which is the default and changes nothing.
        self.sentiment: Any = None
        #: A veto-only entry filter (a meta-label gate, for example). It may refuse a
        #: trade; it may never resize one - sizing belongs to the risk engine.
        self.entry_filter: Any = None
        #: A calibrated `SlippageModel`. When set, entries are capped at the largest size
        #: whose predicted slippage stays inside `execution.max_slippage_bps`.
        self.slippage_model: Any = None
        self._squareoff_done_for: str | None = None
        # fills the venue has booked but this process has not applied yet
        self.unapplied_fills = 0
        self._last_snapshot: datetime | None = None
        self._equity_points = 0
        self._reconcile_state = "ok"
        # Partial audit coverage: this ledger does not intercept every action or enforce
        # permissions at trading and configuration boundaries.
        self.autonomy = AutonomyLedger(store=store, clock=clock)
        beat = Path(config.settings.watchdog.heartbeat_file)
        self._heartbeat_path = beat if beat.is_absolute() else Path(config.settings.state_dir) / beat
        self._heartbeat_warns = 0
        self._last_alert_flush: datetime | None = None
        self.starting_capital = config.settings.starting_capital_usdt
        self.ramp = RampController(config, store)
        self._load_state()
        # Close the crash window between persisting a sixth transition and setting L1.
        self._hold_on_flapping(now=self.clock.now())

    def _restore_health_transitions(self) -> None:
        """Restore the recent health-change window so restarting does not erase flapping."""
        now = self.clock.now()
        rows = self.store.events(limit=-1, kinds=["health_change"], since=now - timedelta(minutes=10))
        for row in reversed(rows):
            payload = row["payload"]
            component = self.health.components.get(payload.get("component"))
            if component is None:
                continue
            try:
                when = parse_iso(row["ts"])
                state = State(payload["to"])
            except (ValueError, KeyError, TypeError):
                continue
            if now - timedelta(minutes=10) <= when <= now:
                component.transitions.append(when)
                component.state = state

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
        killswitch = KillSwitch(store, clock=clock)
        if adapter is None:
            from .execution.adapters import make_adapter

            adapter = make_adapter(config, market, clock, rules, killswitch=killswitch)
            connect = getattr(adapter, "connect", None)
            if connect is not None:  # live / testnet: load markets and market rules
                await connect()
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
        adopt = getattr(self.adapter, "adopt_positions", None)
        if callable(adopt):
            # the venue side must agree with the ledger we just adopted, otherwise every
            # exit from a restored position is rejected as "sell more than held"
            adopt(positions, self.ledger.cash)

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
        """Adapter fills arrive synchronously, possibly while the queue is full.

        The adapter has already moved its own cash by the time we get here, so the fill
        is counted as unapplied until the engine loop books it. Reconciliation must not
        compare the two ledgers in that window: it would report our own queue as a
        mismatch and pause trading.
        """
        self.unapplied_fills += 1
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
            # A candle is *received* when it closes, not when it opened: using its open
            # timestamp put a bars-only replay one bar behind a live feed and made the
            # two paths disagree (caught by `tradebot parity`).
            stamp = getattr(payload, "close_time", None) or getattr(payload, "ts", None)
            if isinstance(stamp, datetime):
                self.clock.set(stamp)
        if kind == "tick":
            await self._on_tick(payload)
        elif kind == "book":
            await self._on_book(payload)
        elif kind == "bar":
            await self._on_bar(payload)
        elif kind == "fill":
            await self._on_fill(payload)
        elif kind == "regime":
            self._log("regime", payload, actor="market")
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
                self._log(
                    "bar_closed",
                    {"symbol": bar.symbol, "timeframe": bar.timeframe, "ts": iso(bar.ts)},
                    actor="market",
                )
        await self._snapshot_equity()
        await self._check_square_off()
        if isinstance(self.clock, SimClock) and self.clock.pace > 0:
            # A *paced* simulated clock drives the dashboard demo: it has no wall time
            # to wait on either, so the periodic health work (staleness, expiry,
            # reconciliation) is driven by market minutes. An unpaced SimClock is a
            # backtest, and a backtest must not depend on wall-clock health policies.
            await self._market_health_check()

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
                self._log(
                    "strategy_error",
                    {"strategy": name, "symbol": bar.symbol, "error": str(exc)},
                    level="CRIT",
                    actor="strategy",
                )
                self._handle_strategy_fault(name, reason=f"exception: {exc}")
                continue
            if signal is None:
                continue
            self.stats.signals += 1
            # A signal belongs to the bar that produced it, not to the wall clock. Both
            # rows are stamped with market time so a backtest's audit trail is ordered
            # like the session it describes and a replay reproduces it exactly.
            if signal.ts is None or utc(signal.ts) > self.clock.now():
                signal = replace(signal, ts=bar.close_time)
            if not self._signal_is_sane(signal):
                continue
            self.store.save_signal(signal)
            self._log(
                "signal",
                {"signal_id": signal.id, "strategy": name, "symbol": signal.symbol, "side": signal.side.value},
                actor="strategy",
                ts=self.clock.now(),
            )
            await self._try_enter(signal, ctx)

    def _signal_is_sane(self, signal: Signal) -> bool:
        """Refuse a signal whose prices are not usable numbers.

        A NaN or infinite stop reaches `save_signal` as a `decimal.InvalidOperation` and
        takes the whole session down - which is not a data problem the operator can see,
        it is a crash. A quarantined signal with a logged reason is the honest version.
        """
        for field_name in ("stop", "target"):
            value = getattr(signal, field_name, None)
            if value is None or not _finite_decimal(value) or value <= 0:
                self._log(
                    "signal_rejected",
                    {
                        "strategy": signal.strategy,
                        "symbol": signal.symbol,
                        "field": field_name,
                        "value": str(value),
                    },
                    level="WARN",
                    actor="strategy",
                )
                return False
        return True

    # ------------------------------------------------------------------
    # entry path
    # ------------------------------------------------------------------
    async def _try_enter(self, signal: Signal, ctx: StrategyContext) -> None:
        # market-hours gate (§18.8): an entry outside the venue's session is not a trade,
        # it is a rejection waiting to happen. Crypto is always open, so this is free.
        session_note = self.market_session_note()
        if session_note is not None:
            self.stats.vetoes += 1
            self.store.save_verdict(signal.id, self._veto("market_closed", session_note))
            self._log(
                "market_closed_veto",
                {"symbol": signal.symbol, "reason": session_note},
                actor="risk",
            )
            return
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
            self._log(
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
        if self.entry_filter is not None and not self._entry_allowed(signal, ctx):
            return
        qty = decision.qty
        if self.sentiment is not None:
            qty = self._apply_sentiment(signal, qty, rules)
        qty = self._apply_slippage_budget(signal, qty, rules)
        if qty <= 0:
            self.stats.vetoes += 1
            self.store.save_verdict(signal.id, self._veto("slippage_budget", "no size fills inside the budget"))
            return

        plan = EntryPlan(
            symbol=signal.symbol,
            strategy=signal.strategy,
            stop=signal.stop,
            target=signal.target,
            signal_id=signal.id,
            atr=D(ctx.atr) if ctx.atr == ctx.atr else ZERO,
        )
        await self.om.submit_entry(signal, qty, price=decision.entry_price, plan=plan)
        self.stats.orders += 1

    def _maybe_calibrate_slippage(self, minimum_fills: int = 30) -> None:
        """Learn the venue's slippage from our own fills, once there are enough of them.

        Thirty fills is a low bar deliberately: the model's job here is only to refuse the
        order that is far too big for the book, and its fallback for an unseen bucket is
        the worst observed value. Until then no cap applies, and the fills are the
        evidence that will justify one.
        """
        if self.slippage_model is not None or len(self.slippage.records) < minimum_fills:
            return
        try:
            self.slippage_model = self.slippage.model()
        except Exception as exc:  # noqa: BLE001 - calibration must never stop trading
            self._log("slippage_model_failed", {"error": type(exc).__name__}, level="WARN", actor="execution")
            return
        self._log(
            "slippage_model_fitted",
            {
                "fills": self.slippage_model.samples,
                "symbols": len(self.slippage_model.buckets),
                "fallback_bps": round(self.slippage_model.fallback_bps, 2),
                "budget_bps": float(self.cfg.settings.execution.max_slippage_bps),
            },
            actor="execution",
        )

    def _apply_slippage_budget(self, signal: Signal, qty: Decimal, rules: Any) -> Decimal:
        """Cap the size at what the calibrated slippage model says the book can absorb.

        A 0.4 BTC order into a book with 0.1 BTC at the touch is not the same trade as a
        0.01 BTC order, and the backtest's book walk is the only place that difference
        currently appears. With a calibrated model the engine refuses the oversized
        version up front instead of discovering it in the fills. No model, no cap: an
        uncalibrated guess would just be a smaller order with no evidence behind it.
        """
        from .core.money import round_qty as _round_qty

        budget = float(getattr(self.cfg.settings.execution, "max_slippage_bps", 0.0) or 0.0)
        if self.slippage_model is None or budget <= 0:
            return qty
        book = self.market.book(signal.symbol)
        if book is None:
            return qty
        try:
            affordable = size_for_budget(
                self.slippage_model, signal.symbol, book, signal.side, budget_bps=budget, max_qty=qty
            )
        except Exception as exc:  # noqa: BLE001 - a broken model must not stop trading
            self._log(
                "slippage_sizing_error",
                {"symbol": signal.symbol, "error": type(exc).__name__},
                level="WARN",
                actor="execution",
            )
            return qty
        capped = _round_qty(affordable, rules.lot_step)
        if capped >= qty:
            return qty
        self._log(
            "slippage_sizing",
            {
                "symbol": signal.symbol,
                "from_qty": str(qty),
                "to_qty": str(capped),
                "budget_bps": budget,
                "best_level_qty": str(book.bids[0].qty if signal.side is Side.BUY and book.bids else (
                    book.asks[0].qty if book.asks else ZERO
                )),
            },
            actor="execution",
        )
        return capped

    def _entry_allowed(self, signal: Signal, ctx: StrategyContext) -> bool:
        """Consult the veto-only filter. A failing filter lets the trade through.

        An exception here means the meta-model or its features are broken; blocking every
        entry because a model file is unreadable would look identical to a market with no
        setups, and nobody would investigate.
        """
        if self.entry_filter is None:  # pragma: no cover - guarded by the caller
            return True
        try:
            allowed, reason = self.entry_filter(signal, ctx)
        except Exception as exc:  # noqa: BLE001 - a broken filter must not stop trading
            self._log(
                "entry_filter_error",
                {"symbol": signal.symbol, "error": type(exc).__name__, "str(exc)": str(exc)[:200]},
                level="WARN",
                actor="filter",
            )
            return True
        if allowed:
            return True
        self.stats.vetoes += 1
        self.store.save_verdict(signal.id, self._veto("entry_filter", reason))
        self._log(
            "entry_filter_veto",
            {"signal_id": signal.id, "strategy": signal.strategy, "symbol": signal.symbol, "reason": reason},
            actor="filter",
        )
        return False

    def _apply_sentiment(self, signal: Signal, qty: Decimal, rules: Any) -> Decimal:
        """Sentiment may shrink an approved size. It may never grow it.

        The risk engine has already decided what is acceptable; this can only take away.
        If the modifier cuts the order to zero lots, the entry is skipped and the reason is
        recorded, because a silently dropped order is worse than a logged one.
        """
        from .core.money import round_qty as _round_qty

        try:
            decision = self.sentiment.modifier(signal.symbol, self.clock.now())
        except Exception as exc:  # noqa: BLE001 - a broken news feed must not stop trading
            self._log("sentiment_error", {"symbol": signal.symbol, "error": str(exc)}, level="WARN",
                      actor="sentiment")
            return qty
        multiplier = min(1.0, max(0.0, float(decision.get("multiplier", 1.0))))
        if multiplier >= 0.999:
            return qty
        reduced = _round_qty(qty * D(str(multiplier)), rules.lot_step)
        self._log(
            "sentiment_sizing",
            {
                "symbol": signal.symbol,
                "from_qty": str(qty),
                "to_qty": str(reduced),
                "multiplier": multiplier,
                "reason": decision.get("reason", ""),
                "sentiment_score": decision.get("score"),
            },
            actor="sentiment",
        )
        return reduced

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
            round_trip_cost_bps=self._round_trip_cost_bps(symbol),
        )

    def _round_trip_cost_bps(self, symbol: str) -> Decimal:
        """Fees for both legs plus expected slippage, in basis points.

        Slippage comes from the calibrated model once there is one; before that it is
        reported as unknown (0) rather than guessed. Fees are never unknown: they are in
        the venue's market rules, and a check that pretends fees do not exist would be
        worse than no check at all.
        """
        try:
            rules = self.adapter.get_market_rules(symbol)
            fees = Decimal(2) * D(str(rules.taker_fee)) * Decimal("10000")
        except Exception:  # noqa: BLE001 - unknown rules mean unknown cost, not a crash
            return ZERO
        slippage = ZERO
        if self.slippage_model is not None:
            # the *expected* fill cost, not the worst one: `fallback_bps` is the stress
            # number the size cap uses, and a cost budget built on it refuses everything
            expected = getattr(self.slippage_model, "expected_bps", None)
            slippage = D(str(expected() if callable(expected) else 0.0))
        return fees + slippage

    def adapter_cash(self) -> Decimal:
        cash = getattr(self.adapter, "cash", None)
        return D(str(cash)) if cash is not None else self.ledger.cash

    # ------------------------------------------------------------------
    # fills and position lifecycle
    # ------------------------------------------------------------------
    async def _on_fill(self, fill: Fill) -> None:
        self.stats.fills += 1
        try:
            self.slippage.observe(fill, book=self.market.book(fill.symbol))
            self._maybe_calibrate_slippage()
        except Exception as exc:  # noqa: BLE001 - measurement must never stop trading
            self._log("slippage_record_failed", {"error": str(exc)}, level="WARN", actor="execution")
        self.unapplied_fills = max(self.unapplied_fills - 1, 0)
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
                self._log(
                    "ledger_error", {"error": str(exc), "fill": fill.to_dict()}, level="CRIT", actor="ledger"
                )
                return
            pos = self.ledger.get(fill.symbol, fill.strategy)
            if pos is not None:
                self.store.save_position(pos)
                self._log(
                    "position_opened",
                    pos.to_dict(fill.price) | {"order_id": fill.order_id},
                    actor="portfolio",
                )
                await self._ensure_protection(pos)
        else:
            try:
                closed = self.ledger.on_fill(fill)
            except ValueError as exc:
                self._log(
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
                self._log(
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
        self._log("trade_closed", trade.to_dict(), actor="portfolio")
        if trade.pnl < 0 and trade.reason in ("stop", "stop_gap", "flatten"):
            self._cooldowns[trade.symbol] = self.clock.now() + timedelta(
                minutes=self.cfg.risk.cooldown_minutes_after_stop
            )
            self._log(
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
                self._log(
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
            self._log(
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
            self._log(
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
        self._exit_attempts[key] = self.clock.now()
        self._log(
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
        if name in self._disabled_strategies:
            return True
        paused = self.store.kv_get("paused_strategies", {}) or {}
        info = paused.get(name)
        if not info:
            return False
        until = info.get("until") if isinstance(info, dict) else None
        if until is None:
            return True
        return self.clock.now() < _parse(until)

    def _handle_strategy_fault(self, name: str, *, reason: str) -> None:
        """Pause a faulty strategy, and after three faults in an hour, disable it (§20.3).

        Restarting a strategy that raises deterministically looks like recovery from the
        outside: it comes back, trades nothing, and raises again. The budget is what makes
        the third failure the last one for the session, and the disable is what a human
        reads later. Open positions are untouched - their stops live in the ledger, not in
        the strategy.
        """
        now = self.clock.now()
        if self.strategy_faults.allow(name, now=now):
            self.strategy_faults.record(name, now=now)
            self._pause_strategy(name, hours=1, reason=reason)
            return
        self._disabled_strategies[name] = reason
        self._pause_strategy(name, hours=24 * 365, reason=f"disabled after 3 faults: {reason}")
        self._log(
            "strategy_disabled",
            {
                "strategy": name,
                "reason": reason,
                "faults_last_hour": self.strategy_faults.used(name, now=now),
            },
            level="CRIT",
            actor="supervisor",
            ts=now,
        )

    def _pause_strategy(self, name: str, *, hours: int, reason: str) -> None:
        paused = self.store.kv_get("paused_strategies", {}) or {}
        paused[name] = {"until": iso(self.clock.now() + timedelta(hours=hours)), "reason": reason}
        self.store.kv_set("paused_strategies", paused)
        self._log(
            "strategy_paused", {"strategy": name, "hours": hours, "reason": reason}, level="WARN", actor="risk"
        )

    # ------------------------------------------------------------------
    # equity, health, kill switch
    # ------------------------------------------------------------------
    async def _snapshot_equity(self, force: bool = False) -> None:
        """One equity row per N market minutes, not one per symbol bar.

        Bars arrive per symbol, so five symbols would otherwise write five identical
        rows per minute. With the feed running hundreds of times faster than real time
        that is the difference between a readable curve and a database that grows by
        tens of megabytes an hour.
        """
        marks = self.marks
        if not marks:
            return
        now = self.clock.now()
        bars = max(int(self.cfg.settings.execution.equity_snapshot_bars), 1)
        gap = timedelta(seconds=timeframe_seconds(self.cfg.settings.base_timeframe) * bars)
        if not force and self._last_snapshot is not None and now - self._last_snapshot < gap:
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

    def _account_risk_context(self, equity: Decimal) -> RiskContext:
        return RiskContext(
            now=self.clock.now(),
            equity=equity,
            starting_capital=self.starting_capital,
            day_start_equity=D(str(self.store.kv_get("day_start_equity", to_str(equity)))),
            week_start_equity=D(str(self.store.kv_get("week_start_equity", to_str(equity)))),
            peak_equity=D(str(self.store.kv_get("peak_equity", to_str(equity)))),
        )
    def _check_account_limits(self, equity: Decimal) -> None:
        for reason, level in self.risk.account_alerts(self._account_risk_context(equity)):
            if self.killswitch.level.rank < level.rank:
                self.killswitch.trigger(level, trigger=_limit_trigger(reason), reason=reason)
                if level.rank >= KillLevel.L2.rank:
                    asyncio.create_task(self.flatten_all(reason))

    def market_session_note(self) -> str | None:
        """Why entries are not allowed right now, or None when the session is open."""
        now = self.clock.now()
        if self.calendar.is_open(now):
            return None
        return f"venue {self.calendar.name} is closed at {iso(now)}"

    async def _check_square_off(self) -> None:
        """Flatten intraday exposure before the bell, once per session, on market time."""
        if self.squareoff is None:
            return
        now = self.clock.now()
        if not self.squareoff.should_flatten(now):
            return
        session_key = now.date().isoformat()
        if self._squareoff_done_for == session_key or not self.ledger.open_positions:
            return
        self._squareoff_done_for = session_key
        self._log(
            "square_off",
            {"reason": "mandatory square-off before the close", "positions": len(self.ledger.open_positions)},
            level="WARN",
            actor="risk",
        )
        await self.flatten_all("square-off")

    def _log(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        level: str = "INFO",
        actor: str = "engine",
        ts: datetime | None = None,
    ) -> int:
        """Every engine event is stamped with *market* time, never the wall clock.

        A backtest or a replay must produce an audit trail ordered like the session it
        describes. Defaulting to `utcnow()` inside the store made a 2024 backtest write
        events dated today, which broke replay comparison and would have quietly corrupted
        any later analysis. Anything that genuinely needs wall time logs it in the payload.
        """
        return self.store.log_event(
            kind, payload, level=level, actor=actor, ts=ts or self.clock.now()
        )

    def _heartbeat(self) -> None:
        """The watchdog watches this. Silence means the bot is wedged or dead.

        It is written twice on purpose: the store row is what the watchdog reads when it
        can read, and the file is what the dead man's switch reads when it cannot. A
        database that is locked or on a full disk still leaves the file's mtime behind, so
        "the bot is gone" is observable from outside the process either way.
        """
        now = self.clock.now()
        self.store.kv_set("heartbeat", iso(now))
        self.store.kv_set("heartbeat_events", self.stats.events)
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self._heartbeat_path.write_text(iso(now), encoding="utf-8")
        except OSError as exc:  # a full disk must not kill the loop, but it is a fault
            if self._heartbeat_warns < 3:
                self._heartbeat_warns += 1
                self._log("heartbeat_file_failed", {"error": str(exc)}, level="WARN")

    async def health_check(self) -> dict[str, Any]:
        now = self.clock.now()
        self._heartbeat()
        await self._flush_alerts(now)
        dq = self.cfg.settings.data_quality
        stale = self.market.stale_symbols(now, dq.stale_symbol_seconds)
        all_stale = self.market.all_stale(now, dq.stale_all_seconds)
        disk = shutil.disk_usage(str(self.cfg.settings.data_dir))
        disk_used = disk.used / disk.total if disk.total else 0.0
        window = timedelta(seconds=self.cfg.risk.exchange_error_rate_window_seconds)
        errors = self.store.count_events("order_error", since=now - window)
        submitted = self.store.count_events("order_submitted", since=now - window)
        attempts = errors + submitted
        error_rate = self._order_error_rate(now)
        min_samples = self.cfg.risk.exchange_error_rate_min_samples

        if all_stale and self.stats.ticks > 0:
            self.killswitch.trigger(
                KillLevel.L1, trigger="stale_feeds", reason=f"all feeds stale > {dq.stale_all_seconds}s"
            )
        if disk_used >= self.cfg.risk.disk_pause_pct:
            self.killswitch.trigger(KillLevel.L1, trigger="disk_full", reason=f"disk {disk_used:.0%} used")
        elif disk_used >= self.cfg.risk.disk_warn_pct:
            self._log("disk_warning", {"used_pct": round(disk_used, 3)}, level="WARN", actor="health")
        if attempts >= min_samples and error_rate >= float(self.cfg.risk.exchange_error_rate_limit):
            self.killswitch.trigger(
                KillLevel.L1,
                trigger="exchange_error_rate",
                reason=(
                    f"{error_rate:.0%} order errors over {window.total_seconds() / 60:.0f} min "
                    f"({errors}/{attempts} attempts)"
                ),
            )
        self._reconcile_state = await self.reconcile()
        await self._maybe_auto_resume(all_stale=all_stale, error_rate=error_rate, disk_used=disk_used)
        if self._reconcile_state != "ok":
            self.killswitch.trigger(
                KillLevel.L1, trigger="reconciliation", reason=str(self._reconcile_state)
            )
        await self.om.expire_stale_entries(now)
        self.killswitch.check_kill_file()
        if self.killswitch.must_flatten() and (self.ledger.open_positions or self.om.pending):
            await self.flatten_all(f"kill switch {self.killswitch.level.value}")
        self._report_component_health(
            now=now, disk_used=disk_used, error_rate=error_rate, all_stale=all_stale
        )
        flapping = self._hold_on_flapping(now=now)
        healed = await self.playbook.tick(self) if self.running else []
        health = {
            "ts": iso(now),
            "flapping_held": flapping,
            "autonomy": self.autonomy.summary(),
            "components": self.health.snapshot(now=now, restart_budget=self.strategy_faults),
            "playbook": self.playbook.as_dict(now=now),
            "healed": [outcome.as_dict() for outcome in healed],
            "stale_symbols": stale,
            "all_stale": all_stale,
            "disk_used_pct": round(disk_used, 3),
            "error_rate": round(error_rate, 3),
            "order_attempts": attempts,
            "reconciliation": self._reconcile_state,
            "kill_level": self.killswitch.level.value,
            "equity": to_str(self.equity(), 8),
            "open_positions": len(self.ledger.open_positions),
            "pending_orders": len(self.om.pending),
        }
        return health

    def _hold_on_flapping(self, *, now: datetime) -> list[str]:
        """§20.4: more than five state changes in ten minutes holds the system at L1.

        A component that keeps collapsing is not healthy between collapses, and the
        auto-resume ladder cannot tell the difference - it sees ten minutes of "fine" and
        lifts the pause, then the component flaps again. So this trigger is deliberately
        *not* in `AUTO_RESUME_TRIGGERS`: the hold is released by a human, not by the clock.
        Optional components are ignored, for the same reason they cannot make the system
        FAILED.
        """
        flapping = self.health.flapping(now=now)
        critical = [name for name in flapping if self.health.components[name].critical]
        if not critical:
            return []
        reason = f"flapping: {', '.join(critical)} changed state more than 5 times in 10 min"
        # The sixth newest transition ages out last. Persist its deadline before
        # returning so a restart or a separate CLI process cannot bypass the hold.
        until: datetime | None = max(
            sorted(self.health.components[name].transitions, reverse=True)[5] + timedelta(minutes=10)
            for name in critical
        )
        existing = self.store.kv_get("health_flapping_hold")
        if existing and until is not None:
            try:
                until = max(until, parse_iso(existing["until"]))
            except (KeyError, TypeError, ValueError):
                # Leave corrupt safety state in place: the kill switch fails closed.
                until = None
        if until is not None:
            self.store.kv_set("health_flapping_hold", {"until": iso(until), "components": critical}, ts=now)
        self._healthy_since = None
        self._healthy_pause_state = None
        if self.killswitch.trigger(KillLevel.L1, trigger="flapping", reason=reason, actor="health"):
            self.autonomy.record("health", "hold_for_flapping", AutonomyLevel.PROTECT, reason, ts=now)
        self._log("health_flapping", {"components": critical}, level="WARN", actor="health")
        return critical

    def _build_playbook(self) -> Playbook:
        """§20.3's rows, as detect / act / verify triples.

        Six rows are wired here; the rest of the table needs a venue to test against
        (dead man's switch, deployment rollback, exchange status page) or an adapter
        method that does not exist yet (server time for clock drift). The unwired rows
        are listed in `docs/BUILD_PLAN_20_21.md` as missing rather than pretended.
        """

        async def _feed_stale(_: Any) -> bool:
            now = self.clock.now()
            return self.market.all_stale(now, self.cfg.settings.data_quality.stale_all_seconds)

        async def _reconnect_feed(_: Any) -> str:
            connect = getattr(self.feed, "connect", None)
            close = getattr(self.feed, "close", None)
            if connect is None:
                return "no reconnect available on this feed"
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()
            await connect()
            self.stats.reconnects += 1
            return "reconnected the feed"

        async def _feed_verified(_: Any) -> bool:
            return bool(getattr(self.feed, "connected", False)) or not self.market.all_stale(
                self.clock.now(), self.cfg.settings.data_quality.stale_all_seconds
            )

        async def _api_pressure(_: Any) -> bool:
            window = timedelta(seconds=self.cfg.risk.exchange_error_rate_window_seconds)
            errors = self.store.count_events("order_error", since=self.clock.now() - window)
            attempts = errors + self.store.count_events("order_submitted", since=self.clock.now() - window)
            if attempts < self.cfg.risk.exchange_error_rate_min_samples:
                return False
            return errors / attempts >= float(self.cfg.risk.exchange_error_rate_limit)

        async def _throttle(_: Any) -> str:
            self.playbook.breaker.open(self.clock.now())
            bucket = getattr(self.om, "bucket", None)
            halved = ""
            if bucket is not None and hasattr(bucket, "rate"):
                bucket.rate = max(1.0, float(bucket.rate) / 2)
                halved = f", request rate halved to {bucket.rate:.1f}/s"
            return f"breaker opened for {self.playbook.breaker.open_seconds:.0f}s{halved}"

        async def _api_verified(_: Any) -> bool:
            """§20.3: verify with a real call, not with the fact that we did something."""
            try:
                await self.adapter.get_open_orders()
            except Exception:  # noqa: BLE001
                return False
            self.playbook.breaker.record_success(self.clock.now())
            return True

        async def _exit_stuck(_: Any) -> bool:
            now = self.clock.now()
            for (symbol, strategy), submitted_at in list(self._exit_attempts.items()):
                if self.ledger.get(symbol, strategy) is None:
                    self._exit_attempts.pop((symbol, strategy), None)
                    self._exit_resubmits.pop((symbol, strategy), None)
                    continue
                if now - submitted_at >= timedelta(seconds=self.cfg.settings.execution.exit_stuck_seconds):
                    return True
            return False

        async def _resubmit_exit(_: Any) -> str:
            now = self.clock.now()
            retried = 0
            for (symbol, strategy), submitted_at in list(self._exit_attempts.items()):
                pos = self.ledger.get(symbol, strategy)
                if pos is None or now - submitted_at < timedelta(
                    seconds=self.cfg.settings.execution.exit_stuck_seconds
                ):
                    continue
                self._exiting.pop((symbol, strategy), None)  # the guard is what blocked the retry
                self._exit_resubmits[(symbol, strategy)] = self._exit_resubmits.get((symbol, strategy), 0) + 1
                await self._exit_position(pos, "heal:stuck_exit")
                retried += 1
            return f"re-submitted {retried} stuck exit(s) at market"

        async def _exit_gone(_: Any) -> bool:
            """Verified when nothing is still stuck.

            Position closed counts. A *fresh* re-submission also counts, because a market
            exit fills through our own fill queue and is not closed at the instant it is
            sent. What does not count is the third re-submission of the same position: at
            that point the venue is not honouring exits we can see, and the row must fail
            so the ladder takes it to L2 instead of retrying until the heat death of the
            universe.
            """
            now = self.clock.now()
            ttl = timedelta(seconds=self.cfg.settings.execution.exit_stuck_seconds)
            for (symbol, strategy), submitted_at in list(self._exit_attempts.items()):
                if self.ledger.get(symbol, strategy) is None:
                    continue
                if now - submitted_at >= ttl:
                    return False
                if self._exit_resubmits.get((symbol, strategy), 0) > 2:
                    return False
            return True

        async def _unprotected(_: Any) -> bool:
            if self.cfg.mode is Mode.PAPER:
                return False  # paper has no venue-side stop; the local monitor is the protection
            now = self.clock.now()
            return any(
                pos.stop_order_id is None and self._exiting.get((pos.symbol, pos.strategy), now) <= now
                for pos in self.ledger.open_positions
            )

        async def _protect(_: Any) -> str:
            protected = 0
            for pos in list(self.ledger.open_positions):
                if pos.stop_order_id is not None:
                    continue
                await self._ensure_protection(pos)
                protected += int(pos.stop_order_id is not None)
            return f"placed {protected} protective stop(s)"

        async def _protection_verified(_: Any) -> bool:
            return all(pos.stop_order_id is not None for pos in self.ledger.open_positions)

        async def _disk_pressure(_: Any) -> bool:
            usage = shutil.disk_usage(str(self.cfg.settings.data_dir))
            return usage.used / usage.total >= self.cfg.risk.disk_warn_pct

        async def _purge(_: Any) -> str:
            from .data.archive import Archive

            keep = int(self.cfg.settings.archive.keep_days) if hasattr(self.cfg.settings, "archive") else 30
            archive = Archive(str(self.cfg.settings.archive_dir or self.cfg.settings.data_dir / "archive"))
            removed = archive.apply_retention(keep_days=keep, kind="books")
            return f"purged {len(removed)} book partition(s) older than {keep} days"

        async def _disk_verified(_: Any) -> bool:
            usage = shutil.disk_usage(str(self.cfg.settings.data_dir))
            return usage.used / usage.total < self.cfg.risk.disk_warn_pct

        async def _reconcile_broken(_: Any) -> bool:
            return self._reconcile_state not in ("ok", None, "")

        async def _reconcile(_: Any) -> str:
            """Re-reconcile, and if the venue holds an order we do not know, cancel it.

            Re-running the comparison alone is not a repair - it reports the same
            disagreement again. `reconcile()` already excludes resting protective stops and
            reduce-only exits from "unknown", so what is left is an entry order we lost
            track of, and cancelling is both allowed by §20.1 and the safe direction.
            """
            state = await self.reconcile()
            cancelled = 0
            if state.startswith("unknown open orders:"):
                for chunk in state.split(":", 1)[1].split(","):
                    symbol, _, client_id = chunk.strip().rpartition(":")
                    if not client_id:
                        continue
                    try:
                        await self.adapter.cancel_order(client_id, symbol)
                        cancelled += 1
                    except Exception as exc:  # noqa: BLE001 - the second reconcile decides
                        self._log(
                            "heal_cancel_failed",
                            {"order": client_id, "symbol": symbol, "error": str(exc)},
                            level="WARN",
                            actor="heal",
                        )
                state = await self.reconcile()
            self._reconcile_state = state
            suffix = f" after cancelling {cancelled} unknown order(s)" if cancelled else ""
            return f"reconciled: {state}{suffix}"

        async def _reconcile_verified(_: Any) -> bool:
            return self._reconcile_state == "ok"

        return Playbook(
            [
                Row(
                    name="feed_stale",
                    detect=_feed_stale,
                    action=_reconnect_feed,
                    verify=_feed_verified,
                    escalate=KillLevel.L1,
                    cooldown=timedelta(seconds=30),
                    mttr_target=10.0,
                ),
                Row(
                    name="api_pressure",
                    detect=_api_pressure,
                    action=_throttle,
                    verify=_api_verified,
                    escalate=KillLevel.L1,
                    cooldown=timedelta(minutes=1),
                    mttr_target=60.0,
                ),
                Row(
                    name="exit_stuck",
                    detect=_exit_stuck,
                    action=_resubmit_exit,
                    verify=_exit_gone,
                    escalate=KillLevel.L2,
                    cooldown=timedelta(seconds=30),
                    mttr_target=5.0,
                ),
                Row(
                    name="position_unprotected",
                    detect=_unprotected,
                    action=_protect,
                    verify=_protection_verified,
                    escalate=KillLevel.L2,
                    cooldown=timedelta(seconds=30),
                    mttr_target=5.0,
                ),
                Row(
                    name="disk_pressure",
                    detect=_disk_pressure,
                    action=_purge,
                    verify=_disk_verified,
                    escalate=KillLevel.L1,
                    cooldown=timedelta(minutes=30),
                    mttr_target=30.0,
                ),
                Row(
                    name="reconcile_mismatch",
                    detect=_reconcile_broken,
                    action=_reconcile,
                    verify=_reconcile_verified,
                    escalate=KillLevel.L1,
                    cooldown=timedelta(minutes=2),
                    mttr_target=10.0,
                ),
            ],
            clock=self.clock,
            on_event=self._heal_event,
            escalate=self._escalate_heal,
        )

    def _escalate_heal(self, level: KillLevel, trigger: str, reason: str) -> None:
        # Raising a kill level only ever removes risk, which is a level-3 (protect) action.
        if self.killswitch.trigger(level, trigger=trigger, reason=reason, actor="heal"):
            self.autonomy.record(
                "heal", f"kill_{level.value.lower()}", AutonomyLevel.PROTECT, f"{trigger}: {reason}",
                ts=self.clock.now(),
            )

    #: Heal rows that can close or protect a position are level 3; the rest are repairs.
    PROTECTIVE_ROWS = frozenset({"exit_stuck", "position_unprotected"})

    def _heal_event(self, kind: str, payload: dict[str, Any]) -> None:
        row = str(payload.get("row", ""))
        self.autonomy.record(
            "heal",
            f"{kind}:{row}" if row else kind,
            AutonomyLevel.PROTECT if row in self.PROTECTIVE_ROWS else AutonomyLevel.CORRECT,
            str(payload.get("action") or payload.get("detail") or "")[:500],
            outcome="ok" if kind in ("heal", "heal_escalation") else "failed",
            ts=self.clock.now(),
        )
        level = str(payload.pop("level", "INFO"))
        self._log(kind, payload, level=level, actor="heal", ts=self.clock.now())

    async def _flush_alerts(self, now: datetime) -> None:
        """Retry undelivered alerts, at most once a minute.

        A queue that is retried on every tick hammers a transport that is already down; one
        that is never retried is a queue nobody reads.
        """
        if self.notifier is None or not hasattr(self.notifier, "flush"):
            return
        if self._last_alert_flush is not None and (now - self._last_alert_flush).total_seconds() < 60:
            return
        self._last_alert_flush = now
        with contextlib.suppress(Exception):
            await self.notifier.flush()

    def _alerts_independent(self) -> bool:
        """One transport is a single point of failure for the alarm system (§20.5)."""
        router = getattr(self.notifier, "router", None)
        return bool(router is not None and router.independent)

    def _alert_transport_detail(self) -> str:
        if self.notifier is None:
            return "no notifier: alerts go to the event log only"
        router = getattr(self.notifier, "router", None)
        if router is None:
            return "single transport (telegram)"
        names = [c.name for c in router.remote_channels if getattr(c, "configured", True)]
        if not names:
            return "file floor only: no remote transport configured"
        if not self._alerts_independent():
            return f"one remote transport ({', '.join(names)}): set ALERT_WEBHOOK_URL for a second"
        return f"transports: {', '.join(names)}"

    def _report_component_health(
        self,
        *,
        now: datetime,
        disk_used: float,
        error_rate: float,
        all_stale: bool,
    ) -> None:
        """Fill in §20.2's three questions for every component, once per health check.

        The thresholds are deliberately blunt. A health check that needs tuning before it
        can tell you the feed is dead is worse than no health check: these are the values
        that separate "working" from "not working", not a quality score.
        """
        dq = self.cfg.settings.data_quality
        limit = float(self.cfg.risk.exchange_error_rate_limit)
        stale = self.market.stale_symbols(now, dq.stale_symbol_seconds)
        # A process that has not started receiving yet is not a sick feed. Without this a
        # one-shot `tradebot health` on a cold engine reports every symbol stale and calls
        # the system DEGRADED, which trains the reader to ignore the word.
        not_sampled = not self.running and self.stats.ticks == 0 and self.stats.bars == 0
        sample_note = "not sampled: the engine is not running" if not_sampled else ""
        faults = self.store.count_events("strategy_error", since=now - timedelta(minutes=10))
        paused = self.store.kv_get("paused_strategies", {}) or {}
        pending = len(self.om.pending)
        overdue = sum(1 for entry in self.om.pending.values() if entry.deadline < now)

        self.health.report(
            "feed",
            now=now,
            stream=Check(
                liveness=True if not_sampled else (bool(self.feed.running) or not self.running),
                readiness=True if not_sampled else not all_stale,
                correctness=True if not_sampled else not stale,
                detail=sample_note or (f"{len(stale)} stale symbol(s)" if stale else ""),
            ),
        )
        self.health.report(
            "candles",
            now=now,
            builder=Check(
                liveness=True if not_sampled else (self.stats.bars > 0 or not self.running),
                readiness=True
                if not_sampled
                else bool(self.cfg.symbol_names()) and self.market.price(self.cfg.symbol_names()[0]) is not None,
                correctness=True if not_sampled else not all_stale,
                detail=sample_note,
            ),
        )
        self.health.report(
            "strategies",
            now=now,
            workers=Check(
                liveness=not self._disabled_strategies,
                readiness=not paused,
                correctness=faults == 0,
                detail="; ".join(
                    part
                    for part in (
                        f"disabled {sorted(self._disabled_strategies)}" if self._disabled_strategies else "",
                        f"paused {sorted(paused)}" if paused else "",
                        f"{faults} fault(s) in 10 min" if faults else "",
                    )
                    if part
                ),
            ),
        )
        self.health.report(
            "orders",
            now=now,
            queue=Check(
                liveness=getattr(self.adapter, "connected", True),
                readiness=overdue == 0,
                correctness=self.om.stats.fills <= self.om.stats.submitted,
                detail=f"{overdue} of {pending} entry(ies) past their deadline" if overdue else "",
            ),
        )
        self.health.report(
            "store",
            now=now,
            write=Check(
                readiness=disk_used < self.cfg.risk.disk_pause_pct,
                correctness=self._reconcile_state == "ok",
                detail=f"disk {disk_used:.0%}" if disk_used >= self.cfg.risk.disk_warn_pct else "",
            ),
        )
        self.health.report(
            "adapter",
            now=now,
            api=Check(
                liveness=getattr(self.adapter, "connected", True),
                readiness=error_rate < limit,
                correctness=self._reconcile_state == "ok",
                detail=f"error rate {error_rate:.0%}" if error_rate >= limit else "",
            ),
        )
        self.health.report(
            "alerting",
            now=now,
            transport=Check(
                readiness=self.notifier is not None,
                correctness=self._alerts_independent(),
                detail=self._alert_transport_detail(),
            ),
        )
        self.health.report(
            "ai",
            now=now,
            model=Check(
                readiness=True,
                correctness=len(self._disabled_strategies) == 0,
                detail="",
            ),
        )
        # the audit trail (§20.4): a state change is an event, not a field nobody reads
        for change in self.health.changes[self._logged_changes :]:
            self._log(
                "health_change",
                {
                    "component": change["component"],
                    "from": change["from"],
                    "to": change["to"],
                    "detail": change["detail"],
                },
                level="WARN" if change["to"] != "HEALTHY" else "INFO",
                actor="health",
                ts=now,
            )
        self._logged_changes = len(self.health.changes)

    #: L1 causes that may lift on their own once the condition has been gone for
    #: `auto_resume_min_healthy_seconds` (plan §: infrastructure only). A risk breach is
    #: not in here: the daily limit is handled at the day boundary, and the weekly limit
    #: and the drawdown lockdown need a human to review and reset.
    AUTO_RESUME_TRIGGERS = frozenset(
        {"stale_feeds", "exchange_error_rate", "reconciliation", "disk_full"}
    )

    async def _maybe_auto_resume(
        self,
        *,
        all_stale: bool | None = None,
        error_rate: float | None = None,
        disk_used: float | None = None,
        now: datetime | None = None,
    ) -> None:
        """Lift an L1 pause caused by a transient infrastructure fault.

        The plan is specific about this: only L1, only for feed/API/network causes, and
        only after at least ten minutes of sustained health. L2 and L3 never lift, and a
        pause this engine does not recognise is left to the operator. The evidence can
        be passed in (the health check already computed it) or measured here.
        """
        pause_state = self.killswitch.state
        if pause_state.get("level") != KillLevel.L1.value:
            self._healthy_since = None
            self._healthy_pause_state = None
            return
        trigger = str(pause_state.get("trigger", ""))
        if trigger not in self.AUTO_RESUME_TRIGGERS:
            self._healthy_since = None
            self._healthy_pause_state = None
            return

        now = now or self.clock.now()
        if all_stale is None:
            all_stale = self.market.all_stale(now, self.cfg.settings.data_quality.stale_all_seconds)
        if error_rate is None:
            error_rate = self._order_error_rate(now)
        if disk_used is None:
            disk = shutil.disk_usage(str(self.cfg.settings.data_dir))
            disk_used = disk.used / disk.total if disk.total else 0.0

        # Recovery of the original cause is insufficient if another incident arose
        # while already at L1 (same-level escalation does not replace its trigger).
        # Unknown/unhealthy reconciliation, stale feeds, errors, or disk block recovery.
        equity = self.equity()
        healthy = (
            not all_stale
            and error_rate < float(self.cfg.risk.exchange_error_rate_limit)
            and self._reconcile_state == "ok"
            and disk_used < self.cfg.risk.disk_warn_pct
            and equity > 0
            and not self.risk.account_alerts(self._account_risk_context(equity))
        )
        if not healthy:
            self._healthy_since = None
            self._healthy_pause_state = None
            return

        if (self._healthy_since is None or self._healthy_pause_state != pause_state
                or now < self._healthy_since):
            self._healthy_since = now
            self._healthy_pause_state = pause_state
            return
        required = float(self.cfg.settings.execution.auto_resume_min_healthy_seconds)
        held = (now - self._healthy_since).total_seconds()
        if held < required:
            return
        self._resume(reason=f"{trigger} cleared; health held {held / 60:.0f} min", now=now,
                     expected_state=pause_state)

    def _resume_daily_limit_at_day_boundary(self, now: datetime) -> None:
        """The daily loss limit is scoped to a day, so a new day lifts it.

        `_check_day_boundary` re-bases `day_start_equity`, which makes the limit
        satisfied again. Nothing else may clear it: a weekly loss or a drawdown needs a
        human, which is why only the `daily_loss` trigger is handled here. This runs off
        market time, so it behaves identically in a backtest and in a paced demo.
        """
        pause_state = self.killswitch.state
        if pause_state.get("level") != KillLevel.L1.value:
            return
        trigger = str(pause_state.get("trigger", ""))
        if trigger != "daily_loss":
            return
        equity = self.equity()
        day_start = D(str(self.store.kv_get("day_start_equity", to_str(equity))))
        if day_start > 0 and (equity - day_start) / day_start <= self.cfg.risk.daily_loss_stop_new_entries:
            return
        self._resume(reason="new trading day; daily loss limit reset", now=now, expected_state=pause_state)

    def _resume(self, *, reason: str, now: datetime, expected_state: dict[str, Any]) -> None:
        # A pre-existing L1 may have a different trigger; never lift it while a
        # critical component is flapping, even at the daily-loss boundary.
        if any(self.health.components[name].critical for name in self.health.flapping(now=now)):
            self._healthy_since = None
            self._healthy_pause_state = None
            return
        # An L1 from infrastructure can mask a later same-level weekly/daily loss.
        # Recheck all account limits immediately before attempting any automatic L0.
        equity = self.equity()
        if equity <= 0 or self.risk.account_alerts(self._account_risk_context(equity)):
            self._healthy_since = None
            self._healthy_pause_state = None
            self._log("autonomy_refused", {"actor": "engine", "action": "auto_resume",
                                         "reason": "account risk remains or equity unavailable"},
                      level="WARN", actor="engine", ts=now)
            return
        trigger = str(expected_state.get("trigger", ""))
        if self.killswitch.auto_resume(reason=reason, expected_state=expected_state):
            self._healthy_since = None
            self._healthy_pause_state = None
            self.autonomy.record(
                "engine", "auto_resume", AutonomyLevel.PROTECT, f"L1 {trigger} lifted: {reason}", ts=now
            )
            self._reconcile_state = self._reconcile_state or "ok"
            self._log(
                "kill_switch_auto_resume",
                {
                    "trigger": trigger,
                    "reason": reason,
                    "equity": to_str(self.equity(), 2),
                    "market_time": iso(now),
                },
                level="WARN",
                actor="engine",
                ts=now,
            )

    def _order_error_rate(self, now: datetime) -> float:
        window = timedelta(seconds=self.cfg.risk.exchange_error_rate_window_seconds)
        errors = self.store.count_events("order_error", since=now - window)
        submitted = self.store.count_events("order_submitted", since=now - window)
        attempts = errors + submitted
        return errors / attempts if attempts else 0.0

    async def reconcile(self) -> str:
        """Compare the ledger with the venue. Any mismatch is an incident."""
        if self.unapplied_fills > 0:
            # the venue is ahead of us by design: our own fill queue has not drained
            return "ok"
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
        known = {entry.order.client_order_id for entry in self.om.pending.values()}
        # a resting protective stop is ours, and it lives on the venue by design
        known |= {pos.stop_order_id for pos in self.ledger.open_positions if pos.stop_order_id}
        exit_types = (OrderType.LIMIT, OrderType.STOP_MARKET, OrderType.STOP_LIMIT)
        unknown = [
            o
            for o in open_orders
            if o.status.is_open
            and o.client_order_id not in known
            and not (o.reduce_only and o.type in exit_types)
        ]
        if unknown:
            detail = "unknown open orders: " + ", ".join(f"{o.symbol}:{o.client_order_id}" for o in unknown)
            self.store.add_incident("L1", "reconciliation", detail, {"orders": [o.symbol for o in unknown]})
            return detail
        return "ok"

    async def flatten_all(self, reason: str) -> int:
        """L2/L3: cancel everything and close every position at market."""
        self._log("flatten_start", {"reason": reason}, level="CRIT", actor="killswitch")
        cancelled = await self.adapter.cancel_all()
        closed = 0
        for pos in list(self.ledger.open_positions):
            await self._exit_position(pos, "flatten", price=self.market.price(pos.symbol))
            closed += 1
        self._log(
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
        try:
            await self.emit_daily_report(day_end=now)
        except Exception as exc:  # noqa: BLE001 - a report bug must never stop trading
            log.exception('{"event":"daily_report_failed"}')
            self._log(
                "daily_report_failed",
                {"error": str(exc)[:300], "date": day},
                level="CRIT",
                actor="reporting",
                ts=now,
            )
        self._last_day = day
        self.store.kv_set("last_day", day)
        self.store.kv_set("day_start_equity", to_str(equity, 8))
        self.om.reset_windows()
        self._resume_daily_limit_at_day_boundary(now)
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
            system_health={
                **self.health.snapshot(now=now, restart_budget=self.strategy_faults),
                "playbook": self.playbook.metrics(),
            },
        )
        self._attach_ai_notes(report)
        paths = write_reports(
            report, self.cfg.settings.reports_dir, tax_ledger=tax_ledger_rows(report.closed_trades)
        )
        self._log(
            "daily_report",
            {"date": report.date, "net_pnl": to_str(report.net_pnl, 8), "paths": paths},
            actor="reporting",
        )
        if self.notifier is not None:
            try:
                await self.notifier.send(report.telegram(), level="INFO")
            except Exception as exc:  # noqa: BLE001
                self._log("alert_failed", {"error": str(exc)}, level="WARN", actor="alerts")
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
        self._log(
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
        self._log("engine_stop", self.stats.to_dict(), actor="engine")
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

    async def _market_health_check(self) -> None:
        """Run the health check at most once per `health_check_seconds` of market time."""
        interval = max(float(self.cfg.settings.execution.health_check_seconds), 1.0)
        now = self.clock.now()
        if (
            self._last_health_check is not None
            and (now - self._last_health_check).total_seconds() < interval
        ):
            return
        self._last_health_check = now
        try:
            await self.health_check()
        except Exception:  # noqa: BLE001 - health must never kill the trading loop
            log.exception('{"event":"health_check_failed"}')

    # ------------------------------------------------------------------
    # controls (dashboard / CLI / telegram all call these)
    # ------------------------------------------------------------------
    async def pause(self, reason: str = "manual pause", actor: str = "operator") -> None:
        self.killswitch.trigger(KillLevel.L1, trigger="manual", reason=reason, actor=actor)

    async def kill(self, reason: str = "manual kill", actor: str = "operator") -> None:
        self.killswitch.trigger(KillLevel.L2, trigger="manual_kill", reason=reason, actor=actor)
        await self.flatten_all(reason)

    async def resume(
        self, reason: str = "manual resume", actor: str = "operator", *, l1_only: bool = False,
    ) -> None:
        if l1_only:
            reviewed = self.killswitch.state
            if reviewed.get("level") != KillLevel.L1.value:
                raise PermissionError("remote resume may only clear L1; L2/L3 require local review")
            self.killswitch.reset(reason=reason, actor=actor, expected_state=reviewed)
        else:
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


#: Reason text -> the limit that produced it. The reason strings are written in
#: RiskEngine.account_alerts; naming the limit lets recovery treat the daily limit (which
#: resets at midnight) differently from the weekly limit and the drawdown lockdown (which
#: need a human).
_LIMIT_TRIGGERS = (
    ("daily loss", "daily_loss"),
    ("weekly loss", "weekly_loss"),
    ("drawdown", "drawdown_limit"),
)


def _limit_trigger(reason: str) -> str:
    lowered = reason.lower()
    for prefix, trigger in _LIMIT_TRIGGERS:
        if lowered.startswith(prefix):
            return trigger
    return "account_limit"


def _parse(text: str) -> datetime:
    return utc(datetime.fromisoformat(str(text).replace("Z", "+00:00")))


__all__ = ["Engine", "EngineStats", "JsonMessage", "LiveGuardChecker", "KillLevel"]
