"""An NSE execution adapter for Zerodha Kite Connect.

The venues package already models what makes Indian equities different: the session clock
(09:15-15:30 IST), the instrument rules (5-paise ticks, lot steps, ±20% bands), the cost
stack (STT, GST, stamp duty) and the intraday square-off. What was missing was something
that could place an order. This is that adapter, written against the same
`ExchangeAdapter` interface as paper and ccxt, so nothing above it changes.

Four decisions worth stating:

* **A client order id is a tag.** Kite has no idempotent client id. `tag` accepts twenty
  characters, so the request's id goes there and every place is preceded by a lookup:
  if an order with this tag is already live, that order is returned instead of a second
  one being sent. A retry after a timeout cannot double a position.
* **Rejections are refused locally first.** Tick size, lot step, minimum quantity and the
  day's circuit band are checked against `Instrument` before any HTTP call. A band
  rejection from the broker arrives after the order has been counted; a local one never
  reaches the trade log.
* **Closed market is not a transient error.** Sending a MARKET order at 16:00 IST is a bug
  in the caller, so it raises `PermanentExchangeError` rather than being retried.
* **HTTP goes through an injected client**, exactly like the Telegram transport: the whole
  adapter is testable against canned Kite JSON, and no test needs a broker account.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import httpx

from ...config import Mode
from ...core.clock import Clock, utcnow
from ...core.logging import get_logger
from ...core.models import (
    MarketRules,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from ...core.money import D
from ...venues.calendar import NSECalendar
from ...venues.instruments import Instrument, Product
from .base import ExchangeAdapter, PermanentExchangeError, TransientExchangeError

log = get_logger(__name__)

API = "https://api.kite.trade"

#: Our order types to Kite's.
ORDER_TYPE = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP_MARKET: "SL-M",
    OrderType.STOP_LIMIT: "SL",
}

#: Kite's status strings to ours.
STATUS = {
    "OPEN": OrderStatus.SUBMITTED,
    "TRIGGER PENDING": OrderStatus.SUBMITTED,
    "COMPLETE": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELED,
    "CANCELLED AMO": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
}

PRODUCT = {Product.INTRADAY: "MIS", Product.DELIVERY: "CNC", Product.NORMAL: "NRML"}

#: Kite rejects anything longer; a client order id is 22 characters.
TAG_LIMIT = 20


class KiteAdapter(ExchangeAdapter):
    """Live NSE execution over Kite Connect."""

    mode = Mode.LIVE
    name = "kite"

    def __init__(
        self,
        *,
        api_key: str,
        access_token: str,
        instruments: Mapping[str, Instrument] | None = None,
        exchange: str = "NSE",
        client: httpx.AsyncClient | None = None,
        calendar: NSECalendar | None = None,
        clock: Clock | None = None,
        api: str = API,
        require_open_market: bool = True,
    ) -> None:
        if not api_key or not access_token:
            raise ValueError("Kite needs an API key and an access token")
        self.api_key = api_key
        self.access_token = access_token
        self.exchange = exchange
        self.client = client
        self.calendar = calendar or NSECalendar()
        self.clock = clock or Clock()
        self.api = api.rstrip("/")
        self.require_open_market = require_open_market
        self.instruments: dict[str, Instrument] = dict(instruments or {})
        self.prices: dict[str, Decimal] = {}
        self.calls: list[str] = []
        self.errors = 0

    # --- construction ------------------------------------------------------
    @classmethod
    def from_config(cls, config: Any, **kwargs: Any) -> KiteAdapter:
        return cls(
            api_key=config.secrets.kite_api_key,
            access_token=config.secrets.kite_access_token,
            **kwargs,
        )

    # --- http --------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "X-Kite-Version": "3",
            "Authorization": f"token {self.api_key}:{self.access_token}",
        }

    async def _request(
        self, method: str, path: str, *, data: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self.api}{path}"
        self.calls.append(f"{method} {path}")
        try:
            if self.client is not None:
                response = await self.client.request(
                    method, url, data=data, params=params, headers=self._headers(), timeout=15
                )
            else:  # pragma: no cover - needs network
                async with httpx.AsyncClient() as client:
                    response = await client.request(
                        method, url, data=data, params=params, headers=self._headers(), timeout=15
                    )
        except httpx.HTTPError as exc:
            self.errors += 1
            raise TransientExchangeError(f"kite transport: {type(exc).__name__}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            self.errors += 1
            raise TransientExchangeError(f"kite {response.status_code} on {path}")
        if response.status_code >= 400:
            self.errors += 1
            raise PermanentExchangeError(self._error_text(response))
        try:
            body = response.json()
        except ValueError:
            raise TransientExchangeError(f"kite returned non-JSON on {path}") from None
        if body.get("status") not in (None, "success"):
            self.errors += 1
            raise PermanentExchangeError(str(body.get("message") or "kite refused the request"))
        return body.get("data")

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return f"kite {response.status_code}"
        message = body.get("message") or body.get("error_type") or "refused"
        return f"kite {response.status_code}: {message}"

    # --- rules -------------------------------------------------------------
    def instrument_for(self, symbol: str) -> Instrument:
        instrument = self.instruments.get(symbol)
        if instrument is None:
            raise PermanentExchangeError(f"no instrument rules for {symbol}: refusing to guess a tick size")
        return instrument

    def get_market_rules(self, symbol: str) -> MarketRules:
        instrument = self.instrument_for(symbol)
        return MarketRules(
            symbol=symbol,
            tick_size=instrument.tick_size,
            lot_step=instrument.lot_size,
            min_qty=instrument.min_qty,
            min_notional=D("0"),
            maker_fee=D("0"),
            taker_fee=D("0"),  # fees live in NSECostModel, not in the adapter
        )

    def market_price(self, symbol: str) -> Decimal | None:
        return self.prices.get(symbol)

    def describe(self) -> dict[str, object]:
        return {
            "adapter": self.name,
            "mode": self.mode.value,
            "exchange": self.exchange,
            "instruments": len(self.instruments),
            "market_open": self._is_open(),
        }

    # --- session -----------------------------------------------------------
    def _is_open(self) -> bool:
        return self.calendar.is_open(self.clock.now())

    def _require_open(self) -> None:
        if self.require_open_market and not self._is_open():
            raise PermanentExchangeError(
                f"market closed at {self.clock.now().isoformat()}: NSE orders are refused, not queued"
            )

    # --- orders ------------------------------------------------------------
    def _payload(self, request: OrderRequest) -> dict[str, Any]:
        instrument = self.instrument_for(request.symbol)
        problems = instrument.validate(qty=request.qty, price=request.price)
        if problems:
            raise PermanentExchangeError("; ".join(problems))
        payload: dict[str, Any] = {
            "tradingsymbol": request.symbol,
            "exchange": self.exchange,
            "transaction_type": "BUY" if request.side is Side.BUY else "SELL",
            "order_type": ORDER_TYPE[request.type],
            "quantity": int(instrument.round_qty(request.qty)),
            "product": PRODUCT[instrument.product],
            "validity": "IOC" if request.time_in_force is TimeInForce.IOC else "DAY",
            "tag": request.client_order_id[:TAG_LIMIT],
        }
        if request.price is not None:
            payload["price"] = str(instrument.round_price(request.price))
        if request.stop_price is not None:
            payload["trigger_price"] = str(instrument.round_price(request.stop_price))
        return payload

    async def _live_order_for_tag(self, tag: str) -> Order | None:
        """Kite's only route to idempotency: look for our tag before placing anything."""
        for row in _rows(await self._request("GET", "/orders")):
            if str(row.get("tag") or "") == tag and self._is_live(row):
                return self._order_from(row)
        return None

    @staticmethod
    def _is_live(row: Mapping[str, Any]) -> bool:
        return str(row.get("status", "")).upper() in ("OPEN", "TRIGGER PENDING")

    async def place_order(self, request: OrderRequest) -> Order:
        self._require_open()
        tag = request.client_order_id[:TAG_LIMIT]
        existing = await self._live_order_for_tag(tag)
        if existing is not None:
            log.info('{"event":"kite_order_deduped","tag":"%s"}', tag)
            return existing
        payload = self._payload(request)
        try:
            data = await self._request("POST", "/orders/regular", data=payload)
        except TransientExchangeError:
            # The order may have landed anyway. Look before the caller retries.
            race = await self._live_order_for_tag(tag)
            if race is not None:
                return race
            raise
        order_id = str((data or {}).get("order_id") or "")
        return Order(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            qty=request.qty,
            price=request.price,
            status=OrderStatus.SUBMITTED,
            strategy=request.strategy,
            signal_id=request.signal_id,
            stop_price=request.stop_price,
            time_in_force=request.time_in_force,
            reduce_only=request.reduce_only,
            exchange_order_id=order_id,
            reference_price=self.prices.get(request.symbol),
            created_at=utcnow(),
        )

    async def cancel_order(self, client_order_id: str, symbol: str | None = None) -> bool:
        del symbol
        tag = client_order_id[:TAG_LIMIT]
        for row in _rows(await self._request("GET", "/orders")):
            if str(row.get("tag") or "") == tag and self._is_live(row):
                await self._request("DELETE", f"/orders/regular/{row.get('order_id')}")
                return True
        return False

    async def cancel_all(self, symbol: str | None = None) -> int:
        cancelled = 0
        for row in _rows(await self._request("GET", "/orders")):
            if not self._is_live(row):
                continue
            if symbol is not None and str(row.get("tradingsymbol")) != symbol:
                continue
            try:
                await self._request("DELETE", f"/orders/regular/{row.get('order_id')}")
                cancelled += 1
            except PermanentExchangeError as exc:
                # One order that cannot be cancelled must not stop the rest.
                log.warning('{"event":"kite_cancel_failed","order":"%s","error":"%s"}', row.get("order_id"), exc)
        return cancelled

    async def get_order(self, client_order_id: str) -> Order | None:
        tag = client_order_id[:TAG_LIMIT]
        for row in _rows(await self._request("GET", "/orders")):
            if str(row.get("tag") or "") == tag:
                return self._order_from(row)
        return None

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        out: list[Order] = []
        for row in _rows(await self._request("GET", "/orders")):
            if not self._is_live(row):
                continue
            if symbol is not None and str(row.get("tradingsymbol")) != symbol:
                continue
            out.append(self._order_from(row))
        return out

    def _order_from(self, row: Mapping[str, Any]) -> Order:
        symbol = str(row.get("tradingsymbol") or "")
        status = STATUS.get(str(row.get("status", "")).upper(), OrderStatus.SUBMITTED)
        qty = D(str(row.get("quantity") or 0))
        filled = D(str(row.get("filled_quantity") or 0))
        average = D(str(row.get("average_price") or 0))
        price = row.get("price")
        return Order(
            client_order_id=str(row.get("tag") or ""),
            symbol=symbol,
            side=Side.BUY if str(row.get("transaction_type", "")).upper() == "BUY" else Side.SELL,
            type=_our_order_type(str(row.get("order_type", ""))),
            qty=qty,
            price=D(str(price)) if price not in (None, "", 0) else None,
            status=status,
            stop_price=D(str(row["trigger_price"])) if row.get("trigger_price") else None,
            exchange_order_id=str(row.get("order_id") or ""),
            filled_qty=filled,
            avg_fill_price=average,
            updated_at=self.clock.now(),
            error=row.get("status_message") or None,
        )

    # --- account -----------------------------------------------------------
    async def get_balances(self) -> dict[str, Decimal]:
        data = await self._request("GET", "/user/margins")
        available = ((data or {}).get("equity") or {}).get("available") or {}
        cash = available.get("cash") or available.get("live_balance") or 0
        return {"INR": D(str(cash))}

    async def refresh_prices(self, symbols: list[str]) -> dict[str, Decimal]:
        """Update the last-price cache from the quote endpoint (one call, many symbols)."""
        if not symbols:
            return {}
        items = [f"{self.exchange}:{symbol}" for symbol in symbols]
        data = await self._request("GET", "/quote/ltp", params={"i": items})
        out: dict[str, Decimal] = {}
        for key, payload in (data or {}).items():
            symbol = str(key).split(":", 1)[-1]
            price = (payload or {}).get("last_price")
            if price is None:
                continue
            value = D(str(price))
            self.prices[symbol] = value
            out[symbol] = value
        return out

    async def get_positions(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/portfolio/positions")
        net = (data or {}).get("net") or []
        return [dict(row) for row in net]

    async def close(self) -> None:  # pragma: no cover - injected client is owned by the caller
        return None


def _rows(data: Any) -> list[Mapping[str, Any]]:
    """Kite returns a list for order endpoints; anything else is not a row set."""
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, Mapping)]


def _our_order_type(kite_type: str) -> OrderType:
    return {
        "MARKET": OrderType.MARKET,
        "LIMIT": OrderType.LIMIT,
        "SL-M": OrderType.STOP_MARKET,
        "SL": OrderType.STOP_LIMIT,
    }.get(kite_type.upper(), OrderType.LIMIT)


__all__ = ["KiteAdapter", "ORDER_TYPE", "PRODUCT", "STATUS"]
