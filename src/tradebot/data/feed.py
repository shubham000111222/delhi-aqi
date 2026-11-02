"""Market data feeds: a deterministic synthetic market, a candle replay, and ccxt live.

Every feed pushes the *same* three event kinds into the engine through one callback::

    await emit("tick", Trade)      # a print: mark price, paper-order settlement
    await emit("book", OrderBook)  # depth: spread and slippage
    await emit("bar",  Candle)     # a *closed* base bar (1m): aggregation + strategies

Because the callback is awaited, the producer can never outrun the engine queue
(backpressure), which is what makes replay reproducible.

``SyntheticFeed`` is a seeded market, not noise: five coins with different
volatilities, a slow regime cycle (bull / range / bear) and realistic wicks and
volumes. Identical seed plus identical timestamp sequence gives identical events,
so a backtest, a paper session and a bug report are all reproducible.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..config import Config
from ..core.clock import Clock, floor_ts, parse_iso, timeframe_seconds, utc
from ..core.logging import get_logger
from ..core.models import BookLevel, Candle, MarketRules, OrderBook, Side, Trade
from ..core.money import D, round_price, round_qty
from .hub import MarketData

log = get_logger(__name__)

#: ``await emit(kind, payload)``
Emit = Callable[[str, Any], Awaitable[None]]

#: quoted starting prices for the seeded market; anything unknown starts at 100
START_PRICES: dict[str, float] = {
    "BTC": 42_000.0,
    "ETH": 2_250.0,
    "SOL": 98.0,
    "BNB": 305.0,
    "XRP": 0.62,
    "ADA": 0.58,
    "DOGE": 0.09,
    "LTC": 72.0,
    "AVAX": 36.0,
    "MATIC": 0.85,
}

#: per-minute volatility, roughly calibrated to 2024 spot crypto
VOLATILITY: dict[str, float] = {
    "BTC": 0.0011,
    "ETH": 0.0014,
    "SOL": 0.0022,
    "BNB": 0.0015,
    "XRP": 0.0019,
    "ADA": 0.0020,
    "DOGE": 0.0026,
    "LTC": 0.0014,
    "AVAX": 0.0023,
    "MATIC": 0.0020,
}

#: base volume per minute, in the base asset
BASE_VOLUME: dict[str, float] = {
    "BTC": 3.2,
    "ETH": 42.0,
    "SOL": 380.0,
    "BNB": 700.0,
    "XRP": 90_000.0,
}

DEFAULT_VOLATILITY = 0.0016
DEFAULT_BASE_VOLUME = 1000.0


class BaseFeed(ABC):
    """Interface the engine needs from any market data source."""

    name = "feed"

    def __init__(self, config: Config, clock: Clock | None = None) -> None:
        self.cfg = config
        self.clock: Clock = clock or Clock()
        self._running = False
        self._rules: dict[str, MarketRules] = self._config_rules()

    # ------------------------------------------------------------------
    @property
    def symbols(self) -> list[str]:
        return self.cfg.symbol_names()

    @property
    def base_timeframe(self) -> str:
        return self.cfg.settings.base_timeframe

    @property
    def base_seconds(self) -> int:
        return timeframe_seconds(self.base_timeframe)

    def _config_rules(self) -> dict[str, MarketRules]:
        """Rules from ``symbols.yaml``. Live feeds replace them from the exchange."""
        paper = self.cfg.settings.paper
        live = self.cfg.settings.live_fees
        rules: dict[str, MarketRules] = {}
        for spec in self.cfg.symbols.symbols:
            rules[spec.symbol] = MarketRules(
                symbol=spec.symbol,
                tick_size=spec.tick_size,
                lot_step=spec.lot_step,
                min_qty=spec.min_qty,
                min_notional=spec.min_notional,
                maker_fee=live.maker if self.cfg.mode.value != "paper" else paper.maker_fee,
                taker_fee=live.taker if self.cfg.mode.value != "paper" else paper.taker_fee,
            )
        return rules

    def market_rules(self) -> dict[str, MarketRules]:
        return self._rules

    # ------------------------------------------------------------------
    @abstractmethod
    async def run(self, emit: Emit) -> None:
        """Run until stopped or the data runs out."""

    def stop(self) -> None:
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def describe(self) -> dict[str, Any]:
        return {"feed": self.name, "symbols": self.symbols, "base_timeframe": self.base_timeframe}


class SyntheticFeed(BaseFeed):
    """Seeded multi-coin market: one bar per base interval, with ticks and depth."""

    name = "synthetic"

    def __init__(self, config: Config, clock: Clock | None = None) -> None:
        super().__init__(config, clock)
        settings = config.settings.synthetic
        self.rng = random.Random(int(settings.seed))
        self.start_ts = parse_iso(settings.start_ts)
        self.regime_mode = settings.equity_regime
        self.ticks_per_bar = 2
        self.book_levels = max(2, min(int(config.settings.paper.book_levels), 20))
        self._assets: dict[str, _Asset] = {
            symbol: _Asset.starting(
                symbol,
                rules=self._rules[symbol],
                phase=self.rng.random(),
                drift=self.rng.random(),
            )
            for symbol in self.symbols
        }
        self.bars_emitted = 0
        self.ticks_emitted = 0
        self.books_emitted = 0

    # ------------------------------------------------------------------
    async def run(self, emit: Emit) -> None:
        self._running = True
        base = self.base_seconds
        ts = self._first_ts()
        start = ts
        wall_start = time.monotonic()
        pace = float(getattr(self.clock, "pace", 0.0) or 0.0)
        try:
            while self._running:
                await self._simulate_bar(ts, base, emit)
                ts = ts + timedelta(seconds=base)
                if pace > 0:
                    # real time to wait until this simulated instant is due
                    due = wall_start + (ts - start).total_seconds() / pace
                    delay = due - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(min(delay, 60.0))
        except asyncio.CancelledError:  # pragma: no cover - engine shutdown path
            raise
        finally:
            self._running = False

    def _first_ts(self) -> datetime:
        """Start where the clock is, not in the past.

        A paced demo and a live paper session both start "now"; a backtest passes a
        ``SimClock`` positioned at ``synthetic.start_ts``, so it starts there instead.
        """
        now = utc(self.clock.now())
        if now < self.start_ts:
            return self.start_ts
        return floor_ts(now, self.base_seconds)

    async def _simulate_bar(self, ts: datetime, seconds: int = 60, emit: Emit | None = None) -> None:
        """Produce one base interval for every symbol: ticks, then depth, then the bar."""
        if emit is None:
            raise ValueError("emit callback is required")
        if not self._running:
            return
        ts = utc(ts)
        for symbol in self.symbols:
            asset = self._assets[symbol]
            candle, ticks, book = self._next_step(asset, ts, seconds)
            for tick in ticks:
                await emit("tick", tick)
                self.ticks_emitted += 1
            await emit("book", book)
            self.books_emitted += 1
            await emit("bar", candle)
            self.bars_emitted += 1

    # ------------------------------------------------------------------
    def _next_step(self, asset: _Asset, ts: datetime, seconds: int) -> tuple[Candle, list[Trade], OrderBook]:
        """Advance one symbol by one interval. The rng draw order is part of the ABI."""
        rules = self._rules[asset.symbol]
        span = seconds / 60.0
        sigma = asset.sigma * math.sqrt(span)
        drift = self._drift(asset, ts) * span
        open_px = asset.price
        close_px = open_px * math.exp(drift + sigma * self.rng.gauss(0.0, 1.0))
        wick = abs(self.rng.gauss(0.0, sigma * 0.7))
        high_px = max(open_px, close_px) * (1.0 + wick)
        low_px = min(open_px, close_px) * (1.0 - wick)
        volume = asset.base_volume * span * math.exp(self.rng.gauss(0.0, 0.45))

        ticks: list[Trade] = []
        last_px = open_px
        count = self.ticks_per_bar
        for index in range(count):
            frac = (index + 1) / (count + 1)
            px = open_px + (close_px - open_px) * frac
            px *= math.exp(self.rng.gauss(0.0, sigma * 0.3))
            px = min(max(px, low_px), high_px)
            qty = max(volume / (count + 1) * math.exp(self.rng.gauss(0.0, 0.5)), 0.0)
            ticks.append(
                Trade(
                    symbol=asset.symbol,
                    ts=ts + timedelta(seconds=round(seconds * frac)),
                    price=self._quantize(px, rules),
                    qty=round_qty(D(repr(qty)), rules.lot_step),
                    taker_side=Side.BUY if px >= last_px else Side.SELL,
                )
            )
            last_px = px

        candle = Candle(
            symbol=asset.symbol,
            timeframe=self.base_timeframe,
            ts=ts,
            open=self._quantize(open_px, rules),
            high=self._quantize(high_px, rules),
            low=self._quantize(low_px, rules),
            close=self._quantize(close_px, rules),
            volume=round_qty(D(repr(volume)), rules.lot_step),
        )
        book = self._book(asset, last_px, rules, ts + timedelta(seconds=seconds))
        asset.price = close_px
        asset.last_bar_ts = ts
        return candle, ticks, book

    def _book(self, asset: _Asset, mid: float, rules: MarketRules, ts: datetime) -> OrderBook:
        """Top-of-book plus decaying depth, sized so paper fills see real slippage."""
        tick = float(rules.tick_size)
        half_spread = max(mid * 0.00015, tick)
        levels = self.book_levels
        depth_usdt = float(self.cfg.settings.paper.book_depth_usdt)
        per_level = max(depth_usdt / levels, 1.0)
        bids: list[BookLevel] = []
        asks: list[BookLevel] = []
        for level in range(1, levels + 1):
            step = half_spread + (level - 1) * max(half_spread, mid * 0.00008)
            bid_px = mid - step
            ask_px = mid + step
            if bid_px <= 0:
                break
            size = per_level / max(mid, 1e-9) * math.exp(self.rng.gauss(0.0, 0.35))
            bids.append(
                BookLevel(
                    price=self._quantize(bid_px, rules, side="buy"),
                    qty=round_qty(D(repr(size)), rules.lot_step),
                )
            )
            asks.append(
                BookLevel(
                    price=self._quantize(ask_px, rules, side="sell"),
                    qty=round_qty(D(repr(size)), rules.lot_step),
                )
            )
        return OrderBook(symbol=asset.symbol, ts=ts, bids=tuple(bids), asks=tuple(asks))

    def _drift(self, asset: _Asset, ts: datetime) -> float:
        """Per-minute expected log return: a slow regime cycle plus a daily rhythm."""
        hours = ts.timestamp() / 3600.0
        mode = self.regime_mode
        if mode == "bull":
            bias = 1.0
        elif mode == "bear":
            bias = -1.0
        elif mode == "range":
            bias = 0.0
        else:  # mixed: 12h bull, 12h range, 12h bear, repeating
            bias = (1.0, 0.0, -1.0)[int(hours // 12) % 3]
        rhythm = math.sin(2.0 * math.pi * (hours % 24.0) / 24.0 + asset.phase * 2.0 * math.pi)
        return (bias * 0.7 + 0.3 * rhythm) * asset.sigma * 0.09

    def _quantize(self, price: float, rules: MarketRules, side: str | None = None) -> Decimal:
        return round_price(D(repr(price)), rules.tick_size, side)


class ReplayFeed(BaseFeed):
    """Replays recorded candles through the same code path, as fast as possible.

    Only ``bar`` events are emitted: a candle carries its own OHLCV, so a strategy
    that needs ticks is telling you it is not replayable.
    """

    name = "replay"

    def __init__(
        self,
        config: Config,
        bars: dict[str, Sequence[Candle]],
        clock: Clock | None = None,
        *,
        pace: float = 0.0,
    ) -> None:
        super().__init__(config, clock)
        self.bars = {symbol: list(candles) for symbol, candles in bars.items()}
        self.pace = pace
        self.emitted = 0

    def _ordered(self) -> Iterable[Candle]:
        order = {symbol: index for index, symbol in enumerate(self.bars)}
        merged: list[Candle] = [bar for candles in self.bars.values() for bar in candles]
        merged.sort(key=lambda bar: (utc(bar.ts), order.get(bar.symbol, 0)))
        return merged

    async def run(self, emit: Emit) -> None:
        self._running = True
        previous: datetime | None = None
        try:
            for bar in self._ordered():
                if not self._running:
                    break
                if self.pace > 0 and previous is not None:
                    gap = (utc(bar.ts) - previous).total_seconds() / self.pace
                    if gap > 0:
                        await asyncio.sleep(min(gap, 30.0))
                await emit("bar", bar)
                self.emitted += 1
                previous = utc(bar.ts)
        finally:
            self._running = False


class CcxtFeed(BaseFeed):
    """Live market data over ccxt (REST polling).

    This is the honest fallback: one ticker sweep, one order book and one candle
    fetch per symbol per poll. The production path is a WebSocket stream with REST
    gap-fill (build plan section 4); the event contract is identical, so swapping
    this class for a streaming one changes nothing downstream.
    """

    name = "ccxt"

    def __init__(self, config: Config, clock: Clock | None = None) -> None:
        super().__init__(config, clock)
        self.exchange: Any = None
        self.connected = False
        self.errors = 0
        self.reconnects = 0
        self.poll_seconds = max(float(config.settings.ccxt.rest_poll_seconds), 1.0)
        self.depth_levels = max(1, min(int(config.settings.ccxt.depth_levels), 50))
        self._last_bar_ts: dict[str, datetime] = {}

    async def connect(self) -> None:
        import ccxt.async_support as ccxt  # imported lazily: paper/backtest never needs it

        settings = self.cfg.settings.ccxt
        if not hasattr(ccxt, settings.exchange_id):
            raise ValueError(f"ccxt has no exchange {settings.exchange_id!r}")
        exchange = getattr(ccxt, settings.exchange_id)(
            {"enableRateLimit": True, "options": {"defaultType": "spot"}}
        )
        if settings.testnet:
            exchange.set_sandbox_mode(True)
        await exchange.load_markets()
        self.exchange = exchange
        self.connected = True
        self._rules.update(self._rules_from_markets())
        log.info('{"event":"feed_connected","exchange":"%s","mode":"%s"}', settings.exchange_id, self.cfg.mode.value)

    def _rules_from_markets(self) -> dict[str, MarketRules]:
        out: dict[str, MarketRules] = {}
        markets = getattr(self.exchange, "markets", {}) or {}
        for symbol in self.symbols:
            market = markets.get(symbol)
            if not market:
                continue
            base = self._rules.get(symbol)
            limits = market.get("limits") or {}
            precision = market.get("precision") or {}
            amount = limits.get("amount") or {}
            cost = limits.get("cost") or {}
            out[symbol] = MarketRules(
                symbol=symbol,
                tick_size=D(repr(precision.get("price") or (base.tick_size if base else D("0.01")))),
                lot_step=D(repr(precision.get("amount") or (base.lot_step if base else D("0.001")))),
                min_qty=D(repr(amount.get("min") or (base.min_qty if base else D("0.001")))),
                min_notional=D(repr(cost.get("min") or (base.min_notional if base else D("5")))),
                maker_fee=D(repr(market.get("maker") or self.cfg.settings.live_fees.maker)),
                taker_fee=D(repr(market.get("taker") or self.cfg.settings.live_fees.taker)),
            )
        return out

    async def run(self, emit: Emit) -> None:
        self._running = True
        try:
            if not self.connected:
                await self.connect()
            while self._running:
                await self._poll(emit)
                await asyncio.sleep(self.poll_seconds)
        except asyncio.CancelledError:  # pragma: no cover - engine shutdown path
            raise
        except Exception as exc:  # noqa: BLE001 - a feed error must not kill the engine silently
            self.errors += 1
            log.error('{"event":"feed_error","feed":"ccxt","error":"%s"}', str(exc)[:200])
            raise
        finally:
            self._running = False
            await self.close()

    async def _poll(self, emit: Emit) -> None:
        symbols = [s for s in self.symbols if s in getattr(self.exchange, "symbols", [])]
        if not symbols:
            return
        try:
            tickers = await self.exchange.fetch_tickers(symbols)
        except Exception as exc:  # noqa: BLE001 - one bad poll is not an outage
            self.errors += 1
            log.warning('{"event":"ticker_error","error":"%s"}', str(exc)[:160])
            tickers = {}
        for symbol in symbols:
            ticker = tickers.get(symbol)
            if ticker and ticker.get("last") is not None:
                await emit(
                    "tick",
                    Trade(
                        symbol=symbol,
                        ts=self.clock.now(),
                        price=D(repr(ticker["last"])),
                        qty=D(repr(ticker.get("baseVolume") or 0)),
                        taker_side=Side.BUY if (ticker.get("percentage") or 0) >= 0 else Side.SELL,
                    ),
                )
            await self._emit_book(symbol, emit)
            await self._emit_bars(symbol, emit)

    async def _emit_book(self, symbol: str, emit: Emit) -> None:
        try:
            raw = await self.exchange.fetch_order_book(symbol, limit=self.depth_levels)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            log.warning('{"event":"book_error","symbol":"%s","error":"%s"}', symbol, str(exc)[:160])
            return
        bids = tuple(BookLevel(D(repr(p)), D(repr(q))) for p, q in (raw.get("bids") or [])[: self.depth_levels])
        asks = tuple(BookLevel(D(repr(p)), D(repr(q))) for p, q in (raw.get("asks") or [])[: self.depth_levels])
        if not bids and not asks:
            return
        await emit("book", OrderBook(symbol=symbol, ts=self.clock.now(), bids=bids, asks=asks))

    async def _emit_bars(self, symbol: str, emit: Emit) -> None:
        try:
            rows = await self.exchange.fetch_ohlcv(symbol, self.base_timeframe, limit=3)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            log.warning('{"event":"ohlcv_error","symbol":"%s","error":"%s"}', symbol, str(exc)[:160])
            return
        now = self.clock.now()
        for row in rows or []:
            ts = utc(datetime.fromtimestamp(row[0] / 1000.0, tz=now.tzinfo))
            close_time = ts + timedelta(seconds=self.base_seconds)
            if close_time > now:
                continue  # still forming: strategies may only see closed bars
            if ts <= self._last_bar_ts.get(symbol, datetime.min.replace(tzinfo=now.tzinfo)):
                continue
            await emit(
                "bar",
                Candle(
                    symbol=symbol,
                    timeframe=self.base_timeframe,
                    ts=ts,
                    open=D(repr(row[1])),
                    high=D(repr(row[2])),
                    low=D(repr(row[3])),
                    close=D(repr(row[4])),
                    volume=D(repr(row[5] or 0)),
                ),
            )
            self._last_bar_ts[symbol] = ts

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        if self.exchange is not None:
            with contextlib.suppress(Exception):  # closing must never raise
                await self.exchange.close()
            self.exchange = None
        self.connected = False


class _Asset:
    """Mutable per-symbol state of the synthetic market."""

    __slots__ = ("symbol", "price", "sigma", "base_volume", "phase", "drift", "last_bar_ts")

    def __init__(
        self,
        symbol: str,
        price: float,
        sigma: float,
        base_volume: float,
        phase: float,
        drift: float,
    ) -> None:
        self.symbol = symbol
        self.price = price
        self.sigma = sigma
        self.base_volume = base_volume
        self.phase = phase
        self.drift = drift
        self.last_bar_ts: datetime | None = None

    @classmethod
    def starting(cls, symbol: str, *, rules: MarketRules, phase: float, drift: float) -> _Asset:
        base = symbol.split("/")[0]
        price = START_PRICES.get(base, 100.0)
        return cls(
            symbol=symbol,
            price=price,
            sigma=VOLATILITY.get(base, DEFAULT_VOLATILITY),
            base_volume=BASE_VOLUME.get(base, DEFAULT_BASE_VOLUME * price),
            phase=phase,
            drift=drift,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Asset {self.symbol} px={self.price:.6g} sigma={self.sigma}>"


def _precision_to_step(precision: Any, default: Decimal) -> Decimal:
    """Turn a ccxt precision field into a step size.

    ccxt reports tick/lot precision in one of two ways: as a step (``0.01``) or as a
    number of decimals (``2``). Values below 1 are steps, whole numbers are decimals.
    Anything unusable falls back to the configured default rather than guessing zero.
    """
    if precision is None:
        return default
    try:
        value = D(repr(precision))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return default
    if value <= 0:
        return default
    if value < 1:
        return value
    if value != value.to_integral_value():
        return value
    return Decimal(1).scaleb(-int(value))


def make_feed(config: Config, clock: Clock | None = None) -> BaseFeed:
    """Build the feed the configuration asks for."""
    kind = config.settings.feed.value
    if kind == "ccxt":
        return CcxtFeed(config, clock)
    return SyntheticFeed(config, clock)


def replay_from_market(market: MarketData, symbol: str, timeframe: str) -> list[Candle]:
    """Convenience: the candles a hub has already collected, ready for ``ReplayFeed``."""
    return market.candles(symbol, timeframe).bars


__all__ = [
    "BASE_VOLUME",
    "_precision_to_step",
    "START_PRICES",
    "VOLATILITY",
    "BaseFeed",
    "CcxtFeed",
    "Emit",
    "ReplayFeed",
    "SyntheticFeed",
    "make_feed",
    "replay_from_market",
]
