"""Order lifecycle: idempotent ids, safe retries, no blind resends, full audit trail.

Rules enforced here (section 6):
  * every order carries a deterministic client order id, so a retry cannot duplicate it;
  * on timeout/unknown response the status is queried by client id before anything else;
  * only safe errors (network / 429 / 5xx) are retried, with backoff;
  * rejects are never retried and five in five minutes escalate the kill switch;
  * quantities and prices are snapped to the venue's tick and lot rules;
  * entry orders that do not fill before their deadline are cancelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..config import Config, Mode
from ..core.clock import Clock
from ..core.logging import get_logger
from ..core.models import (
    Fill,
    KillLevel,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Signal,
    TimeInForce,
)
from ..core.money import ZERO, round_price, round_qty, to_str
from ..core.store import Store
from ..risk.killswitch import KillSwitch
from ..risk.live_guards import RampController
from .adapters.base import (
    ExchangeAdapter,
    PermanentExchangeError,
    TransientExchangeError,
)
from .adapters.paper import PaperAdapter
from .rate_limit import SlidingWindowCounter, TokenBucket

log = get_logger(__name__)


@dataclass
class PendingEntry:
    order: Order
    signal_id: str
    deadline: datetime
    plan: Any = None
    attempts: int = 0


@dataclass
class OrderStats:
    submitted: int = 0
    filled: int = 0
    cancelled: int = 0
    rejected: int = 0
    errors: int = 0
    timeouts: int = 0
    total_slippage_bps: Decimal = ZERO
    fills: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        avg_slip = (
            to_str(self.total_slippage_bps / self.fills, 3) if self.fills else "0"
        )
        return {
            "submitted": self.submitted,
            "filled": self.filled,
            "cancelled": self.cancelled,
            "rejected": self.rejected,
            "errors": self.errors,
            "timeouts": self.timeouts,
            "avg_slippage_bps": avg_slip,
        }


class OrderManager:
    def __init__(
        self,
        config: Config,
        adapter: ExchangeAdapter,
        store: Store,
        killswitch: KillSwitch,
        clock: Clock,
        on_fill: Any = None,
    ) -> None:
        self.cfg = config
        self.adapter = adapter
        self._initial_adapter = adapter
        self._initial_adapter_mode = adapter.mode
        self._initial_mode = config.mode
        config.risk.validate_envelope()
        self._risk_snapshot = config.risk.model_dump(mode="json")
        self.store = store
        self.killswitch = killswitch
        self._initial_killswitch = killswitch
        self.clock = clock
        self.exec_cfg = config.settings.execution
        self.on_fill_callback = on_fill
        self.pending: dict[str, PendingEntry] = {}
        self.stats = OrderStats()
        now_fn = clock.monotonic
        self.rejects_window = SlidingWindowCounter(config.risk.rejects_window_seconds, now_fn=now_fn)
        self.orders_window = SlidingWindowCounter(60, now_fn=now_fn)
        self.bucket = TokenBucket(rate=10, period=1.0, now_fn=now_fn)
        self._retry_rng_backoff = self.exec_cfg.retry_backoff_seconds
        self._exit_seq = 0
        self.plans: dict[str, Any] = {}
        if hasattr(adapter, "on_fill"):
            adapter.on_fill = self._handle_adapter_fill

    # --- counters for the risk engine ------------------------------------
    @property
    def orders_last_minute(self) -> int:
        return self.orders_window.count()

    @property
    def orders_today(self) -> int:
        return self.store.count_events("order_submitted", since=self.clock.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ))

    # --- entries ----------------------------------------------------------
    async def submit_entry(
        self,
        signal: Signal,
        qty: Decimal,
        price: Decimal | None = None,
        plan: Any = None,
        *,
        sized_ramp_multiplier: Decimal | None = None,
        sized_strategy_multiplier: Decimal | None = None,
    ) -> Order | None:
        rules = self.adapter.get_market_rules(signal.symbol)
        order_type = signal.entry_type
        px = price if price is not None else signal.limit_price
        if order_type is OrderType.LIMIT and px is None:
            px = self.adapter.market_price(signal.symbol)
        request = OrderRequest(
            symbol=signal.symbol,
            side=signal.side,
            qty=round_qty(qty, rules.lot_step),
            type=order_type,
            price=round_price(px, rules.tick_size, signal.side.value) if px is not None else None,
            time_in_force=TimeInForce.GTC,
            strategy=signal.strategy,
            signal_id=signal.id,
            reason=signal.reason[:200],
            sized_ramp_multiplier=sized_ramp_multiplier,
            sized_strategy_multiplier=sized_strategy_multiplier,
        )
        if plan is not None:
            # register before placement: a limit order can fill inside place_order
            self.plans[request.client_order_id] = plan
        order = await self._place(request)
        if order is None:
            self.plans.pop(request.client_order_id, None)
            return None
        if order.status.is_open:
            deadline = self.clock.now() + _timedelta(self.exec_cfg.entry_timeout_seconds)
            self.pending[order.client_order_id] = PendingEntry(
                order=order, signal_id=signal.id, deadline=deadline, plan=plan
            )
        if order.status is OrderStatus.FILLED:
            self._record_filled_entry(order, plan)
        return order

    async def submit_exit(
        self,
        symbol: str,
        qty: Decimal,
        strategy: str,
        reason: str,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
        signal_id: str = "",
    ) -> Order | None:
        """Exits use market orders by default: getting out beats getting a good price."""
        rules = self.adapter.get_market_rules(symbol)
        self._exit_seq += 1
        request = OrderRequest(
            tag=f"exit:{reason[:24]}:{self._exit_seq}",
            symbol=symbol,
            side=Side.SELL,
            qty=round_qty(qty, rules.lot_step),
            type=order_type,
            price=round_price(price, rules.tick_size, "sell") if price is not None else None,
            strategy=strategy,
            signal_id=signal_id,
            reduce_only=True,
            reason=reason[:200],
        )
        return await self._place(request)

    # --- core placement with retries -------------------------------------
    async def _place(self, request: OrderRequest) -> Order | None:
        if type(request.reduce_only) is not bool:
            self.store.log_event(
                "autonomy_refused", {"action": "invalid_reduce_only", "reason": "reduce_only must be boolean",
                                     "client_order_id": request.client_order_id},
                level="CRIT", actor="execution", ts=self.clock.now(),
            )
            return None

        if not self.bucket.try_acquire():
            wait = self.bucket.time_until()
            log.warning('{"event":"rate_limit_wait","seconds":%.3f}', wait)
            await self.clock.sleep(wait)
            self.bucket.try_acquire()

        if request.reduce_only and request.side is not Side.SELL:
            self.store.log_event(
                "autonomy_refused", {"actor": "execution", "action": "increase_position_size",
                                     "reason": "reduce-only buy is not a long-spot exit",
                                     "client_order_id": request.client_order_id},
                level="CRIT", actor="execution", ts=self.clock.now(),
            )
            return None

        attempt = 0
        while attempt < max(1, self.exec_cfg.retry_attempts):
            # Risk approval may have happened before a rate-limit wait or retry. Re-read
            # the persisted switch immediately before every exposure-increasing submit.
            # Reducing exits must remain possible at L1-L3. This is not a global adapter
            # boundary and cannot cancel a request already in flight at the venue.
            if not request.reduce_only and (
                self.cfg.mode != self._initial_mode
                or self.adapter is not self._initial_adapter
                or self.adapter.mode != self._initial_adapter_mode
                or self._initial_adapter_mode != self._initial_mode
                or self.killswitch is not self._initial_killswitch
                or self.killswitch.mode != self._initial_mode
                or self.cfg.risk.model_dump(mode="json") != self._risk_snapshot
            ):
                self.store.log_event(
                    "autonomy_refused", {"actor": "execution", "action": "edit_runtime_config",
                                         "reason": "runtime risk, mode or adapter changed",
                                         "client_order_id": request.client_order_id},
                    level="CRIT", actor="execution", ts=self.clock.now(),
                )
                return None
            if not request.reduce_only and self._initial_mode is Mode.LIVE:
                try:
                    size_current = RampController(self.cfg, self.store).permits_sized_multiplier(
                        request.sized_ramp_multiplier
                    )
                except (ValueError, ArithmeticError):
                    size_current = False
                if not size_current:
                    self.store.log_event(
                        "autonomy_refused", {"action": "increase_allocation",
                                             "reason": "missing, invalid or stale live sizing multiplier",
                                             "client_order_id": request.client_order_id},
                        level="CRIT", actor="execution", ts=self.clock.now(),
                    )
                    return None
            if not request.reduce_only:
                try:
                    sized = request.sized_strategy_multiplier
                    # Legacy callers without a multiplier assert full size, which only
                    # passes when no stricter persisted reduction exists.
                    sized = Decimal("1") if sized is None else sized
                    valid_size = (isinstance(sized, Decimal) and sized.is_finite() and 0 < sized <= 1
                                  and sized <= self.store.strategy_size_multiplier(request.strategy))
                except (ValueError, ArithmeticError):
                    valid_size = False
                if not valid_size:
                    self.store.log_event(
                        "autonomy_refused", {"action": "increase_position_size", "strategy": request.strategy,
                                             "reason": "invalid or stale strategy size multiplier",
                                             "client_order_id": request.client_order_id},
                        level="CRIT", actor="execution", ts=self.clock.now(),
                    )
                    return None
            if not request.reduce_only and (
                self.store.strategy_paused(request.strategy, now=self.clock.now())
            ):
                self.store.log_event(
                    "autonomy_refused", {"action": "re_enable_strategy", "strategy": request.strategy,
                                         "reason": "strategy is durably disabled or paused",
                                         "client_order_id": request.client_order_id},
                    level="CRIT", actor="execution", ts=self.clock.now(),
                )
                return None
            if not request.reduce_only and not self.killswitch.can_open_entries():
                self.store.log_event(
                    "autonomy_refused",
                    {
                        "actor": "execution",
                        "action": "place_order_without_kill_check",
                        "reason": "kill switch blocks entry",
                        "kill_level": self.killswitch.level.value,
                        "client_order_id": request.client_order_id,
                        "symbol": request.symbol,
                    },
                    level="WARN",
                    actor="execution",
                    ts=self.clock.now(),
                )
                return None
            attempt += 1
            try:
                order = await self.adapter.place_order(request)
                self.stats.submitted += 1
                self.orders_window.add()
                self.store.upsert_order(order)
                self.store.log_event(
                    "order_submitted",
                    order.to_dict() | {"attempt": attempt},
                    actor="execution",
                    ts=self.clock.now(),
                )
                return order
            except TransientExchangeError as exc:
                self.stats.errors += 1
                # A retry that later succeeds is not a venue failure. Log it as a retry,
                # and only record an `order_error` when we actually give up: otherwise a
                # single flaky order inflates the error-rate circuit breaker (one gave-up
                # order can produce three "errors" and trip it on a handful of sends).
                giving_up = attempt >= self.exec_cfg.retry_attempts
                self.store.log_event(
                    "order_error" if giving_up else "order_retry",
                    {"client_order_id": request.client_order_id, "error": str(exc), "attempt": attempt},
                    level="WARN",
                    actor="execution",
                    ts=self.clock.now(),
                )
                # A failed lookup is not proof of absence. The CCXT lookup is backed by
                # process-local orders, so None there cannot establish venue absence.
                try:
                    existing = await self.adapter.get_order(request.client_order_id)
                except Exception:  # noqa: BLE001 - an unknown result must fail closed
                    existing = None
                    lookup_ok = False
                else:
                    lookup_ok = True
                if existing is not None:
                    self.store.upsert_order(existing)
                    self.store.log_event(
                        "order_recovered",
                        {"client_order_id": request.client_order_id, "attempt": attempt},
                        actor="execution",
                        ts=self.clock.now(),
                    )
                    return existing
                # Only the in-process simulator can currently prove an order was not
                # accepted. No production adapter has demonstrated authoritative absence.
                authoritative_absence = (
                    lookup_ok and isinstance(self.adapter, PaperAdapter) and self.adapter.mode is Mode.PAPER
                )
                if not authoritative_absence:
                    self.store.log_event(
                        "order_outcome_unknown",
                        {"client_order_id": request.client_order_id, "symbol": request.symbol,
                         "attempt": attempt, "lookup_failed": not lookup_ok},
                        level="CRIT", actor="execution", ts=self.clock.now(),
                    )
                    self.killswitch.trigger(
                        KillLevel.L1, trigger="order_outcome_unknown",
                        reason=f"cannot establish venue outcome for {request.client_order_id}",
                        actor="execution",
                    )
                    return None
                if attempt >= self.exec_cfg.retry_attempts:
                    self.stats.timeouts += 1
                    log.error(
                        '{"event":"order_gave_up","cid":"%s","error":"%s"}',
                        request.client_order_id,
                        exc,
                    )
                    return None
                await self.clock.sleep(self._retry_rng_backoff * attempt)
            except PermanentExchangeError as exc:
                self.stats.rejected += 1
                self.rejects_window.add()
                self.store.log_event(
                    "order_rejected",
                    {"client_order_id": request.client_order_id, "error": str(exc)},
                    level="WARN",
                    actor="execution",
                    ts=self.clock.now(),
                )
                self._check_reject_storm()
                return None
        return None

    def _check_reject_storm(self) -> None:
        limit = self.cfg.risk.rejects_limit
        if self.rejects_window.count() >= limit:
            self.killswitch.trigger(
                KillLevel.L1,
                trigger="reject_storm",
                reason=f"{self.rejects_window.count()} rejects in {self.cfg.risk.rejects_window_seconds}s",
            )

    async def _safe_get_order(self, client_order_id: str) -> Order | None:
        try:
            return await self.adapter.get_order(client_order_id)
        except Exception:  # noqa: BLE001 - a failed query is not a reason to resend
            return None

    # --- fills ------------------------------------------------------------
    def _handle_adapter_fill(self, fill: Fill) -> None:
        """Called synchronously by the adapter; hands off to the engine loop."""
        if self.on_fill_callback is not None:
            self.on_fill_callback(fill)

    def record_fill(self, fill: Fill, *, pending_id: str | None = None) -> None:
        self.store.add_fill(fill, pending_id=pending_id)
        self.stats.fills += 1
        self.stats.total_slippage_bps += fill.slippage_bps
        order = self.adapter_orders().get(fill.order_id)
        if order is not None:
            self.store.upsert_order(order)
            if order.status is OrderStatus.FILLED:
                self.stats.filled += 1
        self.pending.pop(fill.order_id, None)

    def adapter_orders(self) -> dict[str, Order]:
        orders = getattr(self.adapter, "orders", None)
        return orders if isinstance(orders, dict) else {}

    def _record_filled_entry(self, order: Order, plan: Any) -> None:
        self.pending.pop(order.client_order_id, None)

    def plan_for(self, order_id: str) -> Any:
        """Entry plan for a fill, removed so a later fill cannot reuse it."""
        return self.plans.pop(order_id, None)

    def has_pending_entry(self, symbol: str, strategy: str) -> bool:
        return any(
            e.order.symbol == symbol and e.order.strategy == strategy for e in self.pending.values()
        )

    def reset_windows(self) -> None:
        """Called at the UTC day boundary so per-day caps start clean."""
        self.orders_window.clear()

    # --- maintenance called from the engine loop --------------------------
    async def expire_stale_entries(self, now: datetime | None = None) -> int:
        now = now or self.clock.now()
        cancelled = 0
        for cid, entry in list(self.pending.items()):
            if entry.deadline > now:
                continue
            order = await self._safe_get_order(cid)
            if order is None or not order.status.is_open:
                self.pending.pop(cid, None)
                continue
            if await self.adapter.cancel_order(cid, order.symbol):
                self.stats.cancelled += 1
                cancelled += 1
                self.store.upsert_order(order)
                self.store.log_event(
                    "order_expired",
                    {"client_order_id": cid, "symbol": order.symbol, "reason": "entry timeout"},
                    actor="execution",
                    ts=now,
                )
            self.pending.pop(cid, None)
        return cancelled

    async def cancel_stale(self) -> None:
        """On kill L2/L3 or shutdown: cancel everything that is still open."""
        await self.adapter.cancel_all()
        for order in await self.adapter.get_open_orders():
            self.store.upsert_order(order)
        self.pending.clear()

    def pending_symbols(self) -> set[str]:
        return {e.order.symbol for e in self.pending.values()}


def _timedelta(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)

