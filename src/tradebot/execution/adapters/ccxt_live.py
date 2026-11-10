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

from decimal import Decimal
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
        detail = []
        passed = True
        try:
            permissions = await self.exchange.fetch_permissions()
            info = permissions.get("info", permissions) if isinstance(permissions, dict) else {}
            withdraw = str(info.get("enableWithdrawals", "unknown")).lower()
            if withdraw in ("true", "1"):
                passed = False
                detail.append("withdrawals are ENABLED on this API key")
            elif withdraw == "unknown":
                passed = False
                detail.append("cannot verify withdrawal permission")
            else:
                detail.append("withdrawals disabled")
            ip_restrict = str(info.get("ipRestrict", "unknown")).lower()
            if ip_restrict in ("false", "0"):
                passed = passed and not require_ip_whitelist
                detail.append("IP whitelist is NOT enforced")
            elif ip_restrict == "unknown":
                passed = False
                detail.append("cannot verify IP whitelist")
            else:
                detail.append("IP whitelist enforced")
        except Exception as exc:  # noqa: BLE001 - unverifiable means fail closed
            return {"passed": False, "detail": f"permission check unavailable: {exc}"}
        self._key_permissions = {"passed": passed, "detail": "; ".join(detail)}
        return self._key_permissions

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
        existing = self.orders.get(request.client_order_id)
        if existing is not None and not existing.status.is_terminal:
            return existing
        # Spot strategy is long-only. A caller cannot label a buy as a reducing exit
        # to bypass the entry stop. A sell's label is still NOT proof the venue enforces
        # reduce-only or that a balance/position exists.
        if request.reduce_only and request.side is not Side.SELL:
            self._refuse_order(request, "reduce-only buy is not a supported protective action")
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
            if self.killswitch.store.strategy_disabled(request.strategy):
                self._refuse_order(request, "strategy is durably disabled")
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
            status=_status_of(raw.get("status")),
            strategy=request.strategy,
            signal_id=request.signal_id,
            reduce_only=request.reduce_only,
            reason=request.reason,
            exchange_order_id=str(raw.get("id") or ""),
            filled_qty=D(repr(raw.get("filled") or 0)),
            avg_fill_price=D(repr(raw.get("average") or 0)),
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        order.fee = D(repr((raw.get("fee") or {}).get("cost") or 0))
        self.orders[request.client_order_id] = order
        self.seen_fills[request.client_order_id] = order.filled_qty
        return order

    async def cancel_order(self, client_order_id: str, symbol: str | None = None) -> bool:
        order = self.orders.get(client_order_id)
        if order is None or not order.status.is_open:
            return False
        try:
            await self.exchange.cancel_order(order.exchange_order_id or client_order_id, symbol or order.symbol)
        except Exception as exc:  # noqa: BLE001
            error = self._translate(exc)
            if isinstance(error, PermanentExchangeError):
                order.status = OrderStatus.CANCELED
            raise error from exc
        order.status = OrderStatus.CANCELED
        order.updated_at = self.clock.now()
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
        order.status = _status_of(raw.get("status"))
        order.filled_qty = D(repr(raw.get("filled") or 0))
        order.avg_fill_price = D(repr(raw.get("average") or 0))
        order.updated_at = self.clock.now()
        return order

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
            if not order.status.is_open:
                continue
            try:
                fresh = await self.get_order(cid)
            except Exception:  # noqa: BLE001
                continue
            if fresh is None:
                continue
            already = self.seen_fills.get(cid, ZERO)
            delta = fresh.filled_qty - already
            if delta <= 0:
                continue
            price = fresh.avg_fill_price or (fresh.price or self.market_price(fresh.symbol) or ZERO)
            fee = delta * price * self.get_market_rules(fresh.symbol).taker_fee
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
            self.seen_fills[cid] = fresh.filled_qty
            if self.on_fill is not None:
                self.on_fill(fill)

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
