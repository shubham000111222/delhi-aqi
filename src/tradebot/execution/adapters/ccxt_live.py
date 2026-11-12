"""Live / testnet adapter over ccxt.

It implements exactly the same interface as the paper exchange, so nothing in the
strategy, risk or portfolio layers changes when you switch modes.

**Unproven.** This file has not been run against a real exchange from this
repository (the build sandbox has no exchange connectivity, and running it needs
your keys). Before it touches money: run it against testnet, check the fill
detection against your own exchange app, and leave the live-mode guards on.

Safety choices baked in here:
  * client order ids are sent as ``newClientOrderId`` so retries cannot duplicate;
  * ccxt errors are split into "safe to retry" and "never retry";
  * withdrawals are checked at startup and reported, never used;
  * the adapter refuses to trade a symbol whose market rules it could not load.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

from ...config import Config, Mode
from ...core.clock import Clock, utcnow
from ...core.logging import get_logger
from ...core.models import (
    BookLevel,
    Fill,
    MarketRules,
    Order,
    OrderBook,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from ...core.money import ZERO, D, round_price, round_qty
from ...data.hub import MarketData
from ...risk.killswitch import KillSwitch
from ...risk.live_guards import RampController
from .base import ExchangeAdapter, PermanentExchangeError, TransientExchangeError

log = get_logger(__name__)

_STATUS_MAP = {
    "open": OrderStatus.SUBMITTED,
    "new": OrderStatus.NEW,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


def _status_of(raw: str | None) -> OrderStatus:
    return _STATUS_MAP.get(str(raw or "").lower(), OrderStatus.SUBMITTED)


class CcxtLiveAdapter(ExchangeAdapter):
    def __init__(
        self,
        config: Config,
        market: MarketData,
        clock: Clock,
        *,
        testnet: bool = False,
        exchange: Any = None,
        killswitch: KillSwitch | None = None,
    ) -> None:
        self.cfg = config
        self._initial_mode = Mode.TESTNET if testnet else Mode.LIVE
        config.risk.validate_envelope()
        self._risk_snapshot = config.risk.model_dump(mode="json")
        self.market = market
        self.clock = clock
        self.mode = Mode.TESTNET if testnet else Mode.LIVE
        self.name = f"ccxt:{config.secrets.exchange_id}{'-testnet' if testnet else ''}"
        self.testnet = testnet
        self.exchange = exchange
        self.killswitch = killswitch
        self._initial_killswitch = killswitch
        self.rules: dict[str, MarketRules] = {}
        self.orders: dict[str, Order] = {}
        self.seen_fills: dict[str, Decimal] = {}
        self.seen_fill_notional: dict[str, Decimal] = {}
        self.on_fill: Any = None
        self.quote = config.settings.quote_currency
        self._key_permissions: dict[str, Any] | None = None

    # --- lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        if self.exchange is None:
            import ccxt.async_support as ccxt

            klass = getattr(ccxt, self.cfg.secrets.exchange_id, None)
            if klass is None:
                raise ValueError(f"unknown exchange {self.cfg.secrets.exchange_id!r}")
            key = self.cfg.secrets.testnet_api_key if self.testnet else self.cfg.secrets.api_key
            secret = (
                self.cfg.secrets.testnet_api_secret if self.testnet else self.cfg.secrets.api_secret
            )
            if not key or not secret:
                raise PermissionError(
                    f"missing {'testnet' if self.testnet else 'live'} API credentials in .env"
                )
            self.exchange = klass(
                {
                    "apiKey": key,
                    "secret": secret,
                    "enableRateLimit": True,
                    "timeout": 15000,
                    "options": {"defaultType": "spot"},
                }
            )
            if self.testnet:
                self.exchange.set_sandbox_mode(True)
        await self.exchange.load_markets()
        await self._load_rules()
        log.info('{"event":"live_adapter_connected","adapter":"%s"}', self.name)

    async def _load_rules(self) -> None:
        from ...data.feed import _precision_to_step

        for symbol in self.cfg.symbol_names():
            market = self.exchange.markets.get(symbol)
            if market is None:
                raise ValueError(f"{symbol} is not listed on this venue")
            limits = market.get("limits") or {}
            precision = market.get("precision") or {}
            amount_min = (limits.get("amount") or {}).get("min") or 0
            cost_min = (limits.get("cost") or {}).get("min") or 0
            self.rules[symbol] = MarketRules(
                symbol=symbol,
                tick_size=_precision_to_step(precision.get("price"), D("0.01")),
                lot_step=_precision_to_step(precision.get("amount"), D("0.0001")),
                min_qty=D(repr(amount_min)) if amount_min else D("0.0001"),
                min_notional=D(repr(cost_min)) if cost_min else D("5"),
                maker_fee=D(repr(market.get("maker") or self.cfg.settings.live_fees.maker)),
                taker_fee=D(repr(market.get("taker") or self.cfg.settings.live_fees.taker)),
            )

    async def verify_permissions(self, require_ip_whitelist: bool = True) -> dict[str, Any]:
        """Withdrawals must be off and an IP whitelist must be in place, or we refuse."""
        def parse_flag(value: Any) -> bool | None:
            # Only explicit representations are evidence. Unknown, null, nested and
            # numeric values other than integer 0/1 must not be interpreted as safe.
            if isinstance(value, bool):
                return value
            if type(value) is int and value in (0, 1):
                return bool(value)
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ("true", "1"):
                    return True
                if lowered in ("false", "0"):
                    return False
            return None

        try:
            permissions = await self.exchange.fetch_permissions()
            info = permissions.get("info", permissions) if isinstance(permissions, dict) else None
            if not isinstance(info, dict):
                return {"passed": False, "detail": "cannot verify permission response"}
            withdraw = parse_flag(info.get("enableWithdrawals"))
            restricted = parse_flag(info.get("ipRestrict"))
            if withdraw is None:
                withdrawal_detail = "cannot verify withdrawal permission"
            elif withdraw:
                withdrawal_detail = "withdrawals are ENABLED on this API key"
            else:
                withdrawal_detail = "withdrawals disabled"
            if restricted is None:
                restriction_detail = "cannot verify IP whitelist"
            elif restricted:
                restriction_detail = "IP whitelist enforced"
            else:
                restriction_detail = "IP whitelist is NOT enforced"
            passed = withdraw is False and (restricted is True or (restricted is False and not require_ip_whitelist))
            result = {"passed": passed, "detail": f"{withdrawal_detail}; {restriction_detail}"}
        except Exception:  # noqa: BLE001 - unverifiable means fail closed; avoid leaking venue error details
            result = {"passed": False, "detail": "permission check unavailable"}
        self._key_permissions = result
        return result

    async def close(self) -> None:
        if self.exchange is not None:
            await self.exchange.close()

    # --- orders -----------------------------------------------------------
    def _refuse_order(self, request: OrderRequest, reason: str) -> NoReturn:
        if self._initial_killswitch is not None:
            self._initial_killswitch.store.log_event(
                "autonomy_refused",
                {"actor": "live_adapter", "action": "place_order_without_kill_check",
                 "reason": reason, "client_order_id": request.client_order_id, "symbol": request.symbol},
                level="WARN", actor="live_adapter", ts=self.clock.now(),
            )
        raise PermanentExchangeError(reason)

    async def place_order(self, request: OrderRequest) -> Order:
        if type(request.reduce_only) is not bool:
            self._refuse_order(request, "reduce_only must be boolean")
        if request.reduce_only and request.side is not Side.SELL:
            self._refuse_order(request, "reduce-only buy is not a supported protective action")
        existing = self.orders.get(request.client_order_id)
        if existing is not None:
            # Idempotence means the same intent, not merely the same string ID.
            # Compare submitted fields, not mutable status/fill totals.
            same_intent = (
                (request.symbol, request.side, request.type, request.stop_price,
                 request.reduce_only, request.strategy, request.signal_id)
                == (existing.symbol, existing.side, existing.type, existing.stop_price,
                    existing.reduce_only, existing.strategy, existing.signal_id)
            )
            if same_intent:
                rules = self.get_market_rules(request.symbol)
                same_intent = (
                    round_qty(request.qty, rules.lot_step) == existing.qty
                    and (round_price(request.price, rules.tick_size, request.side.value)
                         if request.price else None) == existing.price
                )
            if not same_intent:
                if self.killswitch is not None:
                    self.killswitch.latch_execution_observation_fault(request.client_order_id)
                self._refuse_order(request, "client ID reused with conflicting order intent")
            # A terminal order is still the outcome; never resubmit it.
            return existing
        # A sell's label is NOT proof that the venue enforces reduce-only or that
        # an independently verified balance/position exists.
        if not request.reduce_only:
            if (self.cfg.mode != self._initial_mode or self.mode != self._initial_mode
                    or self.cfg.risk.model_dump(mode="json") != self._risk_snapshot):
                self._refuse_order(request, "runtime mode or risk configuration changed or mismatched")
            if self.killswitch is None:
                self._refuse_order(request, "kill switch is not attached")
            if self.killswitch is not self._initial_killswitch:
                self._refuse_order(request, "kill switch changed after adapter construction")
            if self.killswitch is not None and self.killswitch.mode != self._initial_mode:
                self._refuse_order(request, "kill switch mode does not match adapter mode")
            if self.killswitch is None or not self.killswitch.can_open_entries():
                self._refuse_order(request, "kill switch blocks entry")
            if self._initial_mode is Mode.LIVE:
                try:
                    size_current = RampController(self.cfg, self.killswitch.store).permits_sized_multiplier(
                        request.sized_ramp_multiplier
                    )
                except (ValueError, ArithmeticError):
                    size_current = False
                if not size_current:
                    self._refuse_order(request, "missing, invalid or stale live sizing multiplier")
            try:
                sized = request.sized_strategy_multiplier
                sized = Decimal("1") if sized is None else sized
                valid_size = (isinstance(sized, Decimal) and sized.is_finite() and 0 < sized <= 1
                              and sized <= self.killswitch.store.strategy_size_multiplier(request.strategy))
            except (ValueError, ArithmeticError):
                valid_size = False
            if not valid_size:
                self._refuse_order(request, "invalid or stale strategy size multiplier")
            if self.killswitch.store.strategy_paused(request.strategy, now=self.clock.now()):
                self._refuse_order(request, "strategy is durably disabled or paused")
        rules = self.get_market_rules(request.symbol)
        qty = round_qty(request.qty, rules.lot_step)
        price = round_price(request.price, rules.tick_size, request.side.value) if request.price else None
        params: dict[str, Any] = {
            "clientOrderId": request.client_order_id,
            "newClientOrderId": request.client_order_id,
        }
        if request.stop_price is not None:
            params["stopPrice"] = float(request.stop_price)
            params["triggerPrice"] = float(request.stop_price)
        if request.type is OrderType.LIMIT and price is None:
            raise PermanentExchangeError("limit order requires a price")
        if self.killswitch is None:
            raise PermanentExchangeError("durable submission tracking requires a kill switch")
        try:
            submission_id = self.killswitch.store.claim_order_submission(
                self.name, request.client_order_id, request_snapshot={
                    "mode": self.mode.value, "symbol": request.symbol,
                    "side": request.side.value, "type": request.type.value,
                    "qty": str(qty), "price": str(price) if price is not None else None,
                    "stop_price": str(request.stop_price) if request.stop_price is not None else None,
                    "reduce_only": request.reduce_only, "strategy": request.strategy,
                    "signal_id": request.signal_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 - never submit without durable intent
            self.killswitch.latch_local_entry_hold()
            raise PermanentExchangeError("could not reserve submission identity") from exc
        try:
            if request.type is OrderType.LIMIT:
                if price is None:
                    raise PermanentExchangeError("limit order requires a price")
                raw = await self.exchange.create_order(
                    request.symbol, "limit", request.side.value, float(qty), float(price), params
                )
            elif request.type is OrderType.MARKET:
                raw = await self.exchange.create_order(
                    request.symbol, "market", request.side.value, float(qty), None, params
                )
            elif request.type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT):
                raw = await self.exchange.create_order(
                    request.symbol,
                    "stop_market" if request.type is OrderType.STOP_MARKET else "stop_limit",
                    request.side.value,
                    float(qty),
                    float(price) if price else None,
                    params | {"stopPrice": float(request.stop_price or price or 0)},
                )
            else:  # pragma: no cover
                raise PermanentExchangeError(f"unsupported order type {request.type}")
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

        order = Order(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            qty=qty,
            price=price,
            stop_price=request.stop_price,
            status=OrderStatus.SUBMITTED,
            strategy=request.strategy,
            signal_id=request.signal_id,
            reduce_only=request.reduce_only,
            reason=request.reason,
            exchange_order_id=str(raw.get("id") or "") if isinstance(raw, dict) else "",
            filled_qty=ZERO,
            avg_fill_price=ZERO,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        self.orders[request.client_order_id] = order
        self.seen_fills[request.client_order_id] = ZERO
        # The venue call succeeded; retain its identity before parsing the response.
        # A malformed response is an unknown outcome, not permission to resubmit.
        self._apply_order_observation(order, raw)
        # Immediate and initial partial fills need the same callback as polled fills.
        # Keep the delta unseen if the callback cannot durably journal it.
        self._emit_observed_fill(request.client_order_id, order)
        try:
            self.killswitch.store.acknowledge_order_submission(submission_id)
        except Exception as exc:  # noqa: BLE001
            self.killswitch.latch_local_entry_hold()
            raise PermanentExchangeError("could not acknowledge submission outcome") from exc
        return order

    async def cancel_order(self, client_order_id: str, symbol: str | None = None) -> bool:
        order = self.orders.get(client_order_id)
        if order is None or not order.status.is_open:
            return False
        try:
            await self.exchange.cancel_order(order.exchange_order_id or client_order_id, symbol or order.symbol)
            # A cancellation acknowledgement is not proof of the final quantity.
            # Query the outcome before reporting success or allowing a terminal cache
            # to hide a fill that raced with cancellation.
            fresh = await self.get_order(client_order_id)
            if fresh is None or fresh.status.is_open:
                raise PermanentExchangeError("cancellation outcome is unconfirmed")
            self._emit_observed_fill(client_order_id, fresh)
        except Exception as exc:  # noqa: BLE001
            if self.killswitch is not None:
                self.killswitch.latch_execution_observation_fault(client_order_id)
            # Neither OrderNotFound nor a timeout proves cancellation. Keep the
            # observed cache state and let recovery inspect it; do not fabricate one.
            raise self._translate(exc) from exc
        return True

    async def cancel_all(self, symbol: str | None = None) -> int:
        count = 0
        for cid, order in list(self.orders.items()):
            if order.status.is_open and (symbol is None or order.symbol == symbol):
                try:
                    await self.cancel_order(cid, order.symbol)
                    count += 1
                except Exception:  # noqa: BLE001
                    continue
        return count

    async def get_order(self, client_order_id: str) -> Order | None:
        order = self.orders.get(client_order_id)
        if order is None:
            return None
        symbol = order.symbol
        try:
            raw = await self.exchange.fetch_order(
                order.exchange_order_id or client_order_id, symbol, {"clientOrderId": client_order_id}
            )
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        self._apply_order_observation(order, raw)
        return order

    def _apply_order_observation(self, order: Order, raw: Any) -> None:
        # Parse before mutating the cached order. A malformed response must not
        # leave a terminal status cached with stale quantities and hide the retry.
        try:
            filled_raw = raw.get("filled")
            average_raw = raw.get("average")
            if isinstance(filled_raw, bool) or isinstance(average_raw, bool):
                raise ValueError("boolean fill total")
            filled = D(str(filled_raw if filled_raw is not None else 0))
            average = D(str(average_raw if average_raw is not None else 0))
            if not filled.is_finite() or filled < self.seen_fills.get(order.client_order_id, ZERO):
                raise ValueError("nonfinite or regressing quantity")
            fee = order.fee
            if raw.get("fee") is not None:
                fee_raw = raw["fee"].get("cost")
                if isinstance(fee_raw, bool):
                    raise ValueError("boolean fee")
                fee = D(str(fee_raw if fee_raw is not None else 0))
                if not fee.is_finite() or fee < 0:
                    raise ValueError("invalid fee")
            if not average.is_finite() or (filled > 0 and average <= 0):
                raise ValueError("invalid cumulative average")
        except (InvalidOperation, TypeError, ValueError, AttributeError) as exc:
            if self.killswitch is not None:
                self.killswitch.latch_execution_observation_fault(order.client_order_id)
            raise PermanentExchangeError(f"invalid cumulative fill for {order.client_order_id}") from exc
        order.status = _status_of(raw.get("status"))
        order.filled_qty = filled
        order.avg_fill_price = average
        order.fee = fee
        order.updated_at = self.clock.now()

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        return [
            o for o in self.orders.values() if o.status.is_open and (symbol is None or o.symbol == symbol)
        ]

    async def get_balances(self) -> dict[str, Decimal]:
        raw = await self.exchange.fetch_balance()
        free = raw.get("free") or {}
        return {asset: D(repr(qty)) for asset, qty in free.items() if qty is not None}

    # --- market data ------------------------------------------------------
    def get_market_rules(self, symbol: str) -> MarketRules:
        if symbol not in self.rules:
            raise KeyError(f"market rules for {symbol} not loaded")
        return self.rules[symbol]

    def market_price(self, symbol: str) -> Decimal | None:
        return self.market.price(symbol)

    async def refresh_book(self, symbol: str, levels: int = 20) -> OrderBook | None:
        raw = await self.exchange.fetch_order_book(symbol, levels)
        bids = tuple(BookLevel(D(repr(p)), D(repr(q))) for p, q, *_ in raw.get("bids", [])[:levels])
        asks = tuple(BookLevel(D(repr(p)), D(repr(q))) for p, q, *_ in raw.get("asks", [])[:levels])
        if not bids or not asks:
            return None
        return OrderBook(symbol=symbol, ts=utcnow(), bids=bids, asks=asks)

    # --- fill polling (the engine awaits this after every market update) ---
    async def drain_pending(self) -> None:
        """Poll tracked orders and emit fills the bot has not seen yet."""
        for cid, order in list(self.orders.items()):
            already = self.seen_fills.get(cid, ZERO)
            if not order.status.is_open:
                # A callback can fail after get_order cached a terminal status. Do not
                # discard that observed delta just because the order is now closed.
                if order.filled_qty <= already:
                    continue
                fresh = order
            else:
                try:
                    observed = await self.get_order(cid)
                except PermanentExchangeError:
                    # Invalid observations have already latched a local entry hold.
                    # Surface them rather than silently treating corruption as a timeout.
                    raise
                except Exception:  # noqa: BLE001
                    continue
                if observed is None:
                    continue
                fresh = observed
            self._emit_observed_fill(cid, fresh)

    def _emit_observed_fill(self, cid: str, fresh: Order) -> None:
        already = self.seen_fills.get(cid, ZERO)
        def invalid(reason: str) -> NoReturn:
            # No fabricated fill may advance the callback watermark. Persist a
            # hold, but do not claim this establishes reconciliation or recovery.
            if self.killswitch is not None:
                self.killswitch.latch_execution_observation_fault(cid)
            raise PermanentExchangeError(f"invalid cumulative fill for {cid}: {reason}")

        quantity = fresh.filled_qty
        if not quantity.is_finite() or quantity < already:
            invalid("nonfinite or regressing quantity")
        delta = quantity - already
        if delta == 0:
            return
        average = fresh.avg_fill_price
        if not average.is_finite() or average <= 0:
            invalid("missing or nonpositive average price")
        notional = quantity * average
        incremental_notional = notional - self.seen_fill_notional.get(cid, ZERO)
        if not incremental_notional.is_finite() or incremental_notional <= 0:
            invalid("nonpositive incremental notional")
        price = incremental_notional / delta
        fee = incremental_notional * self.get_market_rules(fresh.symbol).taker_fee
        # Cumulative averages supply incremental notional, not individual venue
        # trade identities. Fees remain modelled; reconciliation is unverified.
        fill = Fill(
            order_id=cid,
            symbol=fresh.symbol,
            side=fresh.side,
            price=price,
            qty=delta,
            fee=fee,
            ts=self.clock.now(),
            strategy=fresh.strategy,
            liquidity="taker",
            expected_price=fresh.price,
            reason=fresh.reason,
        )
        if self.on_fill is not None:
            self.on_fill(fill)
            self.seen_fills[cid] = quantity
            self.seen_fill_notional[cid] = notional

    # --- helpers ----------------------------------------------------------
    def _translate(self, exc: Exception) -> Exception:
        """Map a ccxt error to retryable or permanent. Unknown errors are permanent."""
        name = type(exc).__name__.lower()
        message = str(exc).lower()
        if "ratelimit" in name or "network" in name or "timeout" in name or "ddos" in name:
            return TransientExchangeError(str(exc))
        if "429" in message or "502" in message or "503" in message or "504" in message:
            return TransientExchangeError(str(exc))
        return PermanentExchangeError(str(exc))
