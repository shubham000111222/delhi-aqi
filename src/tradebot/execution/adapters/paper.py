"""Paper exchange: a virtual account that trades on live prices, deliberately harsh.

Section 6 of the build plan: paper results must not be flattering. So this adapter
walks the real order book (size moves the price), charges fees, adds latency, only
fills limits when price trades *through* the level, and injects rejects and API
failures so the error paths get exercised before real money is involved.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from ...config import Mode, PaperConfig
from ...core.clock import Clock
from ...core.logging import get_logger
from ...core.models import (
    Fill,
    MarketRules,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from ...core.money import ZERO, D, round_price, round_qty
from ...data.hub import MarketData
from ..rate_limit import TokenBucket
from .base import ExchangeAdapter, PermanentExchangeError, TransientExchangeError

log = get_logger(__name__)

FillCallback = Callable[[Fill], None]


class PaperAdapter(ExchangeAdapter):
    mode = Mode.PAPER
    name = "paper"

    def __init__(
        self,
        market: MarketData,
        rules: dict[str, MarketRules],
        cfg: PaperConfig,
        clock: Clock,
        quote_currency: str = "USDT",
        starting_cash: Decimal | None = None,
        seed: int = 0,
    ) -> None:
        self.market = market
        self.rules = rules
        self.cfg = cfg
        self.clock = clock
        self.quote = quote_currency
        self.cash: Decimal = starting_cash if starting_cash is not None else D("1000")
        self.holdings: dict[str, Decimal] = {sym.split("/")[0]: ZERO for sym in rules}
        self.orders: dict[str, Order] = {}
        self._rng = random.Random(seed)
        self._bucket = TokenBucket(rate=20, period=1.0, now_fn=clock.monotonic)
        self.on_fill: FillCallback | None = None
        self.fees_paid: Decimal = ZERO
        self.fills: list[Fill] = []
        self.api_errors = 0
        self.rejects = 0
        self._pending_market: list[Order] = []

    # --- helpers ----------------------------------------------------------
    def _base(self, symbol: str) -> str:
        return symbol.split("/")[0]

    def market_price(self, symbol: str) -> Decimal | None:
        return self.market.price(symbol)

    def get_market_rules(self, symbol: str) -> MarketRules:
        return self.rules[symbol]

    async def get_balances(self) -> dict[str, Decimal]:
        out = {self.quote: self.cash}
        out.update({k: v for k, v in self.holdings.items()})
        return out

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if o.status.is_open and (symbol is None or o.symbol == symbol)
        ]

    async def get_order(self, client_order_id: str) -> Order | None:
        return self.orders.get(client_order_id)

    # --- order entry ------------------------------------------------------
    async def place_order(self, request: OrderRequest) -> Order:
        if type(request.reduce_only) is not bool:
            raise PermanentExchangeError("reduce_only must be boolean")
        if request.reduce_only and request.side is not Side.SELL:
            raise PermanentExchangeError("reduce-only buy is not a supported protective action")
        existing = self.orders.get(request.client_order_id)
        if existing is not None:
            # Idempotency: a retry after a timeout must never create a second order.
            return existing

        if not self._bucket.try_acquire():
            self.api_errors += 1
            raise TransientExchangeError("rate limited by paper exchange")

        if self._rng.random() < float(self.cfg.api_fail_rate):
            self.api_errors += 1
            raise TransientExchangeError("simulated API failure (0.1%)")

        rules = self.get_market_rules(request.symbol)
        qty = round_qty(request.qty, rules.lot_step)
        price = (
            round_price(request.price, rules.tick_size, request.side.value)
            if request.price is not None
            else None
        )
        if qty < rules.min_qty or qty <= 0:
            self.rejects += 1
            raise PermanentExchangeError(
                f"quantity {qty} below lot rules (min {rules.min_qty}) for {request.symbol}"
            )
        notional = (price or self.market_price(request.symbol) or ZERO) * qty
        if notional < rules.min_notional:
            self.rejects += 1
            raise PermanentExchangeError(
                f"notional {notional:.8f} below min notional {rules.min_notional} for {request.symbol}"
            )

        now = self.clock.now()
        # The decision price: what the market looked like when the order was created. Used
        # as the slippage benchmark for market orders, whose "expected price" would
        # otherwise be the fill price itself.
        reference_price = self.market_price(request.symbol)
        order = Order(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            qty=qty,
            price=price,
            reference_price=reference_price,
            stop_price=request.stop_price,
            status=OrderStatus.SUBMITTED,
            strategy=request.strategy,
            signal_id=request.signal_id,
            time_in_force=request.time_in_force,
            reduce_only=request.reduce_only,
            created_at=now,
            updated_at=now,
            reason=request.reason,
            exchange_order_id=f"paper-{len(self.orders) + 1}",
        )
        self.orders[order.client_order_id] = order

        if order.type is OrderType.MARKET:
            await self._execute_market(order)
        elif order.type is OrderType.LIMIT:
            order.status = OrderStatus.SUBMITTED  # waits for price to trade through
            self._try_fill_limit(order)
        elif order.type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT):
            order.status = OrderStatus.SUBMITTED  # waits for the trigger
        return order

    async def cancel_order(self, client_order_id: str, symbol: str | None = None) -> bool:
        order = self.orders.get(client_order_id)
        if order is None or not order.status.is_open:
            return False
        order.status = OrderStatus.CANCELED
        order.updated_at = self.clock.now()
        return True

    async def cancel_all(self, symbol: str | None = None) -> int:
        count = 0
        for order in list(self.orders.values()):
            if order.status.is_open and (symbol is None or order.symbol == symbol):
                order.status = OrderStatus.CANCELED
                order.updated_at = self.clock.now()
                count += 1
        return count

    # --- fills ------------------------------------------------------------
    async def _execute_market(self, order: Order) -> None:
        """Simulate latency, then walk the book so a big order gets a worse price."""
        lo, hi = self.cfg.latency_ms
        latency_ms = self._rng.uniform(lo, hi) if hi > lo else lo
        await self.clock.sleep(latency_ms / 1000.0)
        book = self.market.book(order.symbol)
        mark = self.market_price(order.symbol)
        if mark is None:
            order.status = OrderStatus.REJECTED
            order.error = "no market price"
            self.rejects += 1
            return
        if book is not None and book.bids and book.asks:
            avg, filled, _worst = book.walk(order.side, order.qty)
            if filled <= 0:
                avg, filled = mark, order.qty
        else:
            avg, filled = mark, order.qty
        try:
            # Stress adverse execution relative to the decision reference, including
            # latency and book impact, but do not amplify favorable price movement.
            # Stop references can be old; this is a stress model, not venue evidence.
            factor = self.cfg.slippage_multiplier
            if not factor.is_finite() or factor < 1 or factor > 10:
                raise PermanentExchangeError("invalid paper slippage multiplier")
            reference = order.reference_price if order.reference_price and order.reference_price > 0 else mark
            if order.side is Side.BUY:
                avg += max(ZERO, avg - reference) * (factor - 1)
            else:
                avg -= max(ZERO, reference - avg) * (factor - 1)
            if avg <= 0 or not avg.is_finite():
                raise PermanentExchangeError("stressed market fill price is invalid")
            if filled < order.qty:
                order.status = OrderStatus.PARTIALLY_FILLED
            self._record_fill(order, avg, filled, liquidity="taker")
        except PermanentExchangeError as exc:
            # The venue refused the fill (no stock, no cash). The order must not stay
            # SUBMITTED: it would be returned by get_open_orders() for ever and the
            # engine's reconciliation would (correctly) report it as an unknown order.
            order.with_status(OrderStatus.REJECTED, error=str(exc), updated_at=self.clock.now())
            self.rejects += 1
            raise
        if order.remaining <= 0:
            order.status = OrderStatus.FILLED
        else:
            # Not enough depth: cancel the remainder rather than leaving it resting.
            order.status = OrderStatus.CANCELED
        order.updated_at = self.clock.now()

    def _try_fill_limit(self, order: Order) -> None:
        if order.status.is_terminal or order.type is not OrderType.LIMIT or order.price is None:
            return
        mark = self.market_price(order.symbol)
        if mark is None:
            return
        trade = self.market.last_trade.get(order.symbol)
        ref = trade.price if trade is not None else mark
        # "Fill only when price trades through the level, not merely touches it."
        if order.side is Side.BUY:
            filled = ref < order.price
        else:
            filled = ref > order.price
        if not filled:
            return
        liquidity = "maker"
        self._record_fill(order, order.price, order.remaining, liquidity=liquidity)
        order.status = OrderStatus.FILLED
        order.updated_at = self.clock.now()

    def _record_fill(self, order: Order, price: Decimal, qty: Decimal, liquidity: str) -> None:
        rules = self.get_market_rules(order.symbol)
        fee_rate = rules.maker_fee if liquidity == "maker" else rules.taker_fee
        notional = price * qty
        fee = notional * fee_rate
        base = self._base(order.symbol)
        if order.side is Side.BUY:
            cost = notional + fee
            if cost > self.cash:
                # Cannot happen: risk sizing reserves cash first. Refuse loudly.
                raise PermanentExchangeError(
                    f"insufficient paper cash: need {cost}, have {self.cash}"
                )
            self.cash -= cost
            self.holdings[base] = self.holdings.get(base, ZERO) + qty
        else:
            held = self.holdings.get(base, ZERO)
            if qty > held:
                raise PermanentExchangeError(f"insufficient paper {base}: sell {qty} > hold {held}")
            self.holdings[base] = held - qty
            self.cash += notional - fee
        self.fees_paid += fee

        prev_notional = order.avg_fill_price * order.filled_qty
        new_qty = order.filled_qty + qty
        order.avg_fill_price = (prev_notional + notional) / new_qty if new_qty else price
        order.filled_qty = new_qty
        order.fee += fee
        expected = (
            order.price
            if order.price is not None
            else (order.reference_price if order.reference_price else price)
        )
        slippage_bps = ((price - expected) / expected * D("10000")) if expected else ZERO
        if order.side is Side.SELL:
            slippage_bps = -slippage_bps

        fill = Fill(
            order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            price=price,
            qty=qty,
            fee=fee,
            ts=self.clock.now(),
            strategy=order.strategy,
            liquidity=liquidity,
            slippage_bps=slippage_bps,
            expected_price=expected,
            reason=order.reason,
        )
        self.fills.append(fill)
        if self.on_fill is not None:
            self.on_fill(fill)

    # --- market data hooks (called by the engine/feed) --------------------
    def on_market_update(self, symbol: str) -> None:
        """Called on every tick/book update: settle resting limit and stop orders."""
        for order in list(self.orders.values()):
            if not order.status.is_open or order.symbol != symbol:
                continue
            if order.type is OrderType.LIMIT:
                self._try_fill_limit(order)
            elif order.type is OrderType.STOP_MARKET and order.stop_price is not None:
                mark = self.market_price(symbol)
                if mark is None:
                    continue
                triggered = (
                    mark <= order.stop_price if order.side is Side.SELL else mark >= order.stop_price
                )
                if triggered:
                    order.type = OrderType.MARKET
                    order.updated_at = self.clock.now()
                    self._pending_market.append(order)

    async def drain_pending(self) -> None:
        pending, self._pending_market = self._pending_market, []
        for order in pending:
            if order.status.is_open:
                await self._execute_market(order)

    def adopt_positions(self, positions: list[Any], cash: Decimal) -> None:
        """Take over open positions that survived a restart.

        A fresh adapter knows nothing about yesterday's fills: its ledger starts at the
        configured capital with no stock. Continuing to trade those positions then makes
        every exit fail ("sell more than held"). Crash recovery has to hand the venue
        simulator the same reality the ledger has.
        """
        for pos in positions:
            base = self._base(pos.symbol)
            self.holdings[base] = self.holdings.get(base, ZERO) + pos.qty
        self.cash = cash

    # --- reconciliation view ---------------------------------------------
    async def snapshot(self) -> dict[str, object]:
        return {
            "cash": str(self.cash),
            "holdings": {k: str(v) for k, v in self.holdings.items()},
            "open_orders": [o.client_order_id for o in self.orders.values() if o.status.is_open],
            "fees_paid": str(self.fees_paid),
            "fills": len(self.fills),
        }

    def equity(self) -> Decimal:
        total = self.cash
        for symbol in self.rules:
            base = self._base(symbol)
            qty = self.holdings.get(base, ZERO)
            if qty:
                px = self.market_price(symbol) or ZERO
                total += qty * px
        return total


# Backwards-compatible alias used in the docs/diagrams.
PaperExchange = PaperAdapter
__all__ = ["PaperAdapter", "PaperExchange"]
