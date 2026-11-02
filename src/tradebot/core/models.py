"""Core domain models shared by backtest, paper and live execution.

Everything money-related is Decimal; everything time-related is UTC. There is a
single set of models for all three modes, which is what makes the "one code path"
rule ({@link docs/ARCHITECTURE.md}) enforceable.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .clock import iso, utc
from .money import ZERO, D, money_json, safe_div, to_str


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(StrEnum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)

    @property
    def is_terminal(self) -> bool:
        return not self.is_open


class KillLevel(StrEnum):
    L0 = "L0"  # normal
    L1 = "L1"  # pause: no new entries
    L2 = "L2"  # flatten: cancel everything, close positions
    L3 = "L3"  # lockdown: L2 + manual reset with written reason + 1h cooldown

    @property
    def rank(self) -> int:
        return {"L0": 0, "L1": 1, "L2": 2, "L3": 3}[self.value]


class SignalSide(StrEnum):
    LONG = "long"
    FLAT = "flat"


class ExitReason(StrEnum):
    STOP = "stop"
    TARGET = "target"
    TRAIL = "trail"
    TIME_STOP = "time_stop"
    FLATTEN = "flatten"
    PARTIAL = "partial"
    MANUAL = "manual"
    DECAY = "decay"


class Regime(StrEnum):
    TREND = "trend"
    RANGE = "range"
    HIGH_VOL = "high_vol"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    timeframe: str
    ts: datetime  # bar open time, UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @property
    def close_time(self) -> datetime:
        from .clock import timeframe_seconds

        return self.ts + _timedelta(timeframe_seconds(self.timeframe))

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "ts": iso(self.ts),
            "open": to_str(self.open),
            "high": to_str(self.high),
            "low": to_str(self.low),
            "close": to_str(self.close),
            "volume": to_str(self.volume, 8),
        }


def _timedelta(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    ts: datetime
    price: Decimal
    qty: Decimal
    taker_side: Side = Side.BUY


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    qty: Decimal


@dataclass(frozen=True, slots=True)
class OrderBook:
    symbol: str
    ts: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_pct(self) -> Decimal:
        mid = self.mid
        if mid is None or self.best_bid is None or self.best_ask is None:
            return ZERO
        return (self.best_ask - self.best_bid) / mid

    def notional_depth(self, side: Side, levels: int = 10) -> Decimal:
        book = self.asks if side is Side.BUY else self.bids
        return sum((lv.price * lv.qty for lv in book[:levels]), ZERO)

    def imbalance(self, levels: int = 5) -> Decimal:
        """(bid depth - ask depth) / total depth, a [-1, 1] order book pressure score."""
        bid = sum((lv.qty for lv in self.bids[:levels]), ZERO)
        ask = sum((lv.qty for lv in self.asks[:levels]), ZERO)
        total = bid + ask
        if total == 0:
            return ZERO
        return (bid - ask) / total

    def microprice(self) -> Decimal | None:
        if not self.bids or not self.asks:
            return None
        bb, ba = self.bids[0], self.asks[0]
        total = bb.qty + ba.qty
        if total == 0:
            return self.mid
        return (bb.price * ba.qty + ba.price * bb.qty) / total

    def walk(self, side: Side, qty: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        """Consume the book for `qty` and return (avg_price, filled_qty, worst_price).

        This is how the paper adapter makes size move the price (section 6).
        """
        book = self.asks if side is Side.BUY else self.bids
        remaining = qty
        cost = ZERO
        filled = ZERO
        worst = book[0].price if book else ZERO
        for lv in book:
            take = min(remaining, lv.qty)
            if take <= 0:
                break
            cost += take * lv.price
            filled += take
            worst = lv.price
            remaining -= take
            if remaining <= 0:
                break
        if filled == 0:
            return ZERO, ZERO, ZERO
        return cost / filled, filled, worst

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ts": iso(self.ts),
            "bids": [[to_str(l.price), to_str(l.qty, 8)] for l in self.bids],
            "asks": [[to_str(l.price), to_str(l.qty, 8)] for l in self.asks],
        }


@dataclass(frozen=True, slots=True)
class MarketRules:
    symbol: str
    tick_size: Decimal
    lot_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    maker_fee: Decimal
    taker_fee: Decimal
    max_qty: Decimal = D("1000000")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "tick_size": to_str(self.tick_size),
            "lot_step": to_str(self.lot_step),
            "min_qty": to_str(self.min_qty),
            "min_notional": to_str(self.min_notional),
            "maker_fee": to_str(self.maker_fee),
            "taker_fee": to_str(self.taker_fee),
        }


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy's opinion. Strategies never place orders themselves."""

    id: str
    strategy: str
    symbol: str
    timeframe: str
    side: Side
    entry_type: OrderType
    ts: datetime
    stop: Decimal
    target: Decimal
    confidence: Decimal = D("0.5")
    limit_price: Decimal | None = None
    reason: str = ""
    regime: Regime = Regime.UNKNOWN
    features: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def new_id(strategy: str, symbol: str, ts: datetime) -> str:
        raw = f"{strategy}|{symbol}|{iso(ts)}".encode()
        return hashlib.sha1(raw).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "strategy": self.strategy,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "entry_type": self.entry_type.value,
            "ts": iso(self.ts),
            "stop": money_json(self.stop),
            "target": money_json(self.target),
            "limit_price": money_json(self.limit_price),
            "confidence": to_str(self.confidence, 4),
            "reason": self.reason,
            "regime": self.regime.value,
            "features": self.features,
        }


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: Side
    qty: Decimal
    type: OrderType
    price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    client_order_id: str = ""
    strategy: str = ""
    signal_id: str = ""
    tag: str = ""
    reduce_only: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.client_order_id:
            object.__setattr__(self, "client_order_id", self.make_client_id())

    def make_client_id(self) -> str:
        """Idempotent client order id: retries can never duplicate an order.

        `tag` distinguishes *separate intents* on the same signal (a partial exit and
        the target exit that follows it must not share an id) while a retry of the
        same intent keeps the same id and therefore cannot duplicate.
        """
        raw = f"{self.strategy}|{self.signal_id}|{self.symbol}|{self.side}|{self.type}|{self.tag}"
        digest = hashlib.sha1(raw.encode()).hexdigest()[:20]
        return f"tb{digest}"

    @property
    def notional(self) -> Decimal:
        if self.price is None:
            return ZERO
        return self.price * self.qty


@dataclass(slots=True)
class Order:
    client_order_id: str
    symbol: str
    side: Side
    type: OrderType
    qty: Decimal
    price: Decimal | None
    status: OrderStatus
    strategy: str = ""
    signal_id: str = ""
    stop_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    reduce_only: bool = False
    exchange_order_id: str | None = None
    filled_qty: Decimal = ZERO
    avg_fill_price: Decimal = ZERO
    fee: Decimal = ZERO
    reason: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error: str | None = None

    @property
    def remaining(self) -> Decimal:
        return max(self.qty - self.filled_qty, ZERO)

    @property
    def notional(self) -> Decimal:
        px = self.avg_fill_price or self.price or ZERO
        return px * (self.filled_qty or self.qty)

    def with_status(self, status: OrderStatus, **changes: Any) -> Order:
        """Mutate in place and return self, so references held by the ledger stay live."""
        self.status = status
        for key, value in changes.items():
            setattr(self, key, value)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "exchange_order_id": self.exchange_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "type": self.type.value,
            "qty": to_str(self.qty),
            "price": money_json(self.price),
            "stop_price": money_json(self.stop_price),
            "status": self.status.value,
            "strategy": self.strategy,
            "signal_id": self.signal_id,
            "filled_qty": to_str(self.filled_qty),
            "avg_fill_price": money_json(self.avg_fill_price),
            "fee": money_json(self.fee),
            "created_at": iso(self.created_at) if self.created_at else None,
            "updated_at": iso(self.updated_at) if self.updated_at else None,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    price: Decimal
    qty: Decimal
    fee: Decimal
    ts: datetime
    strategy: str = ""
    liquidity: str = "taker"
    slippage_bps: Decimal = ZERO
    expected_price: Decimal | None = None
    reason: str = ""

    @property
    def notional(self) -> Decimal:
        return self.price * self.qty

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "price": money_json(self.price),
            "qty": to_str(self.qty),
            "fee": money_json(self.fee),
            "ts": iso(self.ts),
            "strategy": self.strategy,
            "liquidity": self.liquidity,
            "slippage_bps": to_str(self.slippage_bps, 2),
            "reason": self.reason,
        }


@dataclass(slots=True)
class Position:
    """A long spot position. Long-only by design (section 18.9)."""

    symbol: str
    strategy: str
    qty: Decimal
    avg_entry: Decimal
    stop: Decimal
    target: Decimal
    initial_stop: Decimal
    opened_at: datetime
    bars_held: int = 0
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    partial_taken: bool = False
    trailing_stop: Decimal | None = None
    atr_at_entry: Decimal = ZERO
    stop_order_id: str | None = None
    risk_per_unit: Decimal = ZERO
    initial_qty: Decimal = ZERO
    highest_price: Decimal = ZERO

    @property
    def notional(self) -> Decimal:
        return self.qty * self.avg_entry

    def unrealized(self, mark: Decimal) -> Decimal:
        return (mark - self.avg_entry) * self.qty

    def r_multiple(self, mark: Decimal) -> Decimal:
        risk = (self.avg_entry - self.initial_stop) * self.initial_qty
        if risk <= 0:
            return ZERO
        return self.unrealized(mark) / risk

    def to_dict(self, mark: Decimal | None = None) -> dict[str, Any]:
        d = {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "qty": to_str(self.qty),
            "avg_entry": money_json(self.avg_entry),
            "stop": money_json(self.effective_stop),
            "target": money_json(self.target),
            "opened_at": iso(self.opened_at),
            "bars_held": self.bars_held,
            "realized_pnl": money_json(self.realized_pnl),
            "fees": money_json(self.fees),
            "partial_taken": self.partial_taken,
            "trailing_stop": money_json(self.trailing_stop),
            "stop_order_id": self.stop_order_id,
        }
        if mark is not None:
            d["mark"] = money_json(mark)
            d["unrealized"] = money_json(self.unrealized(mark))
            d["r_multiple"] = to_str(self.r_multiple(mark), 3)
        return d

    @property
    def effective_stop(self) -> Decimal:
        if self.trailing_stop is not None:
            return max(self.trailing_stop, self.stop)
        return self.stop


@dataclass(frozen=True, slots=True)
class RiskCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass(slots=True)
class RiskVerdict:
    approved: bool
    reason: str = ""
    checks: list[RiskCheck] = field(default_factory=list)
    adjusted_qty: Decimal | None = None

    @property
    def failed(self) -> list[RiskCheck]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "qty": money_json(self.adjusted_qty),
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks],
        }


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    ts: datetime
    equity: Decimal
    cash: Decimal
    positions_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": iso(self.ts),
            "equity": money_json(self.equity),
            "cash": money_json(self.cash),
            "positions_value": money_json(self.positions_value),
            "realized_pnl": money_json(self.realized_pnl),
            "unrealized_pnl": money_json(self.unrealized_pnl),
            "fees": money_json(self.fees),
        }


def new_id(prefix: str = "") -> str:
    token = uuid.uuid4().hex[:12]
    return f"{prefix}{token}" if prefix else token


def fee_rate_for(liquidity: str, rules: MarketRules) -> Decimal:
    return rules.maker_fee if liquidity == "maker" else rules.taker_fee


def bps(a: Decimal, b: Decimal) -> Decimal:
    """Difference in basis points, safe on a zero base."""
    return safe_div(a - b, b) * D("10000")


def utc_or_now(ts: datetime | None) -> datetime:
    from .clock import utcnow

    return utc(ts) if ts is not None else utcnow()
