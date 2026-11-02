"""Market data hub: candle aggregation, integrity guards and freshness tracking.

Everything the bot knows about the market arrives here and only here. The hub is
the single place where data quality is decided (build plan section 4), so a bad tick
can never become a mark price and a missing symbol can never look healthy:

* **Aggregation** - 1m bars are rolled up into 15m/1h buckets on *closed* bars only.
  A bucket is emitted when the first bar of the next bucket arrives, never on a
  partially formed bar, so a strategy can never act on a candle that is still moving.
* **Integrity** - duplicates and out-of-order bars are refused and counted, and a
  price that jumps further than ``spike_pct`` is quarantined instead of becoming the
  mark. Quarantine is loud: it is logged through the summary and the dashboard.
* **Freshness** - every symbol carries the timestamp of its last update; staleness is
  a question the risk/watchdog code asks, never something the hub silently hides.

The hub is purely in-memory and synchronous: the engine's event loop is the only
writer, which keeps ordering deterministic.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..core.clock import floor_ts, iso, timeframe_seconds, utc, utcnow
from ..core.models import Candle, OrderBook, Trade
from ..core.money import ZERO, D, to_str
from .series import CandleSeries

#: age reported for a symbol that has never delivered a single update
NEVER_RECEIVED_SECONDS = 1e9


class _Bucket:
    """An open higher-timeframe candle being built from base bars."""

    __slots__ = ("symbol", "timeframe", "ts", "open", "high", "low", "close", "volume", "count")

    def __init__(self, symbol: str, timeframe: str, ts: datetime, first: Candle) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.ts = ts
        self.open = first.open
        self.high = first.high
        self.low = first.low
        self.close = first.close
        self.volume = first.volume
        self.count = 1

    def add(self, bar: Candle) -> None:
        self.high = max(self.high, bar.high)
        self.low = min(self.low, bar.low)
        self.close = bar.close
        self.volume += bar.volume
        self.count += 1

    def to_candle(self) -> Candle:
        return Candle(
            symbol=self.symbol,
            timeframe=self.timeframe,
            ts=self.ts,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


class TimeframeAggregator:
    """Builds higher timeframes from base bars, emitting only *closed* buckets.

    >>> agg = TimeframeAggregator("BTC/USDT", "1m", ["15m"])
    >>> closed = [c for i in range(16) for c in agg.on_bar(bar_i)]   # doctest: +SKIP

    The bucket timestamp is the bucket's open time, so a 15m candle closed at 00:15
    carries ``ts == 00:00`` - the same convention the exchange uses, which means a
    replayed candle and a live candle are interchangeable.
    """

    def __init__(
        self,
        symbol: str,
        base_timeframe: str,
        timeframes: list[str] | tuple[str, ...],
        *,
        flush_volume_rounding: int = 8,
    ) -> None:
        self.symbol = symbol
        self.base_timeframe = base_timeframe
        self.base_seconds = timeframe_seconds(base_timeframe)
        self.flush_volume_rounding = flush_volume_rounding
        targets: list[tuple[str, int]] = []
        for tf in timeframes:
            if tf == base_timeframe:
                continue
            seconds = timeframe_seconds(tf)
            if seconds % self.base_seconds != 0:
                raise ValueError(
                    f"{tf} ({seconds}s) cannot be derived from {base_timeframe} ({self.base_seconds}s)"
                )
            if seconds < self.base_seconds:
                raise ValueError(f"{tf} is finer than the base timeframe {base_timeframe}")
            targets.append((tf, seconds))
        # shortest first: strategies see 15m before 1h for the same base bar
        self.targets: list[tuple[str, int]] = sorted(set(targets), key=lambda item: item[1])
        self._buckets: dict[str, _Bucket] = {}
        self.closed_count = 0

    # ------------------------------------------------------------------
    def on_bar(self, candle: Candle) -> list[Candle]:
        """Feed one base bar; returns the higher-timeframe candles it closed."""
        if candle.timeframe != self.base_timeframe:
            raise ValueError(
                f"{self.symbol} aggregator expects {self.base_timeframe} bars, got {candle.timeframe}"
            )
        ts = utc(candle.ts)
        closed: list[Candle] = []
        for timeframe, seconds in self.targets:
            bucket_ts = floor_ts(ts, seconds)
            bucket = self._buckets.get(timeframe)
            if bucket is None:
                self._buckets[timeframe] = _Bucket(self.symbol, timeframe, bucket_ts, candle)
                continue
            if bucket_ts == bucket.ts:
                bucket.add(candle)
            elif bucket_ts > bucket.ts:
                closed.append(bucket.to_candle())
                self.closed_count += 1
                self._buckets[timeframe] = _Bucket(self.symbol, timeframe, bucket_ts, candle)
            # bucket_ts < bucket.ts: a late base bar. It was already refused by the
            # series above, so dropping it here keeps history immutable.
        return closed

    def flush(self) -> list[Candle]:
        """Release the open (partial) buckets, e.g. at the end of a replay."""
        out = [bucket.to_candle() for bucket in self._buckets.values() if bucket.count > 0]
        self._buckets.clear()
        self.closed_count += len(out)
        return out

    def open_buckets(self) -> dict[str, int]:
        """Bars accumulated so far per higher timeframe (diagnostics)."""
        return {tf: bucket.count for tf, bucket in self._buckets.items()}


def gap_report(
    series: CandleSeries,
    now: datetime | None = None,
) -> list[tuple[datetime, int]]:
    """Every discontinuity in a series, as ``(first missing bar, missing count)``.

    A feed that drops messages leaves holes; a feed that dies leaves a hole that keeps
    growing. Both are reported here so the health loop can decide on staleness rather
    than discovering an indicator was computed across a gap. ``now`` is optional: when
    given, an open gap between the newest bar and now is reported too.
    """
    bars = series.bars
    if not bars:
        return []
    step = timeframe_seconds(series.timeframe)
    gaps: list[tuple[datetime, int]] = []
    for previous, current in zip(bars, bars[1:], strict=False):
        missing = int((utc(current.ts) - utc(previous.ts)).total_seconds() // step) - 1
        if missing > 0:
            gaps.append((utc(previous.ts) + timedelta(seconds=step), missing))
    if now is not None:
        trailing = int((utc(now) - utc(bars[-1].ts)).total_seconds() // step) - 1
        if trailing > 0:
            gaps.append((utc(bars[-1].ts) + timedelta(seconds=step), trailing))
    return gaps


class MarketData:
    """The single source of truth for prices, candles, books and freshness."""

    def __init__(
        self,
        symbols: list[str] | tuple[str, ...],
        base_timeframe: str = "1m",
        timeframes: list[str] | tuple[str, ...] = ("15m", "1h"),
        *,
        spike_pct: Decimal = D("0.05"),
        series_maxlen: int = 1500,
        quarantine_limit: int = 200,
    ) -> None:
        self.symbols: list[str] = list(dict.fromkeys(symbols))
        self._known: frozenset[str] = frozenset(self.symbols)
        self.base_timeframe = base_timeframe
        self.timeframes: tuple[str, ...] = tuple(timeframes)
        self.spike_pct = D(spike_pct)
        self.series_maxlen = series_maxlen

        # one series per symbol x timeframe (base first so `candles(...)` always works)
        self.series: dict[tuple[str, str], CandleSeries] = {}
        self.aggregators: dict[str, TimeframeAggregator] = {}
        for symbol in self.symbols:
            self.series[(symbol, base_timeframe)] = CandleSeries(symbol, base_timeframe, series_maxlen)
            for tf in self.timeframes:
                if tf != base_timeframe:
                    self.series[(symbol, tf)] = CandleSeries(symbol, tf, series_maxlen)
            self.aggregators[symbol] = TimeframeAggregator(symbol, base_timeframe, self.timeframes)

        self.last_price: dict[str, Decimal] = {}
        self._spike_ref: dict[str, Decimal] = {}
        self.last_trade: dict[str, Trade] = {}
        self.books: dict[str, OrderBook] = {}
        self.received: dict[str, datetime] = {}
        self.quarantined: deque[dict[str, Any]] = deque(maxlen=quarantine_limit)
        self.duplicates = 0
        self.out_of_order = 0
        self.spikes = 0
        self.trades = 0
        self.bars = 0

    # ------------------------------------------------------------------
    # trades and books
    # ------------------------------------------------------------------
    def on_trade(self, trade: Trade) -> bool:
        """Record a trade. Returns False when the tick was rejected as a spike."""
        if trade.symbol not in self._known:
            return False
        price = trade.price
        if price <= 0:
            self._quarantine(trade, "non_positive_price")
            return False
        reference = self._spike_ref.get(trade.symbol) or self.last_price.get(trade.symbol)
        if reference is not None and reference > 0:
            move = abs(price - reference) / reference
            if move > self.spike_pct:
                self._quarantine(trade, f"{float(move) * 100:.2f}% vs {to_str(reference)}", reference)
                return False
        self.last_price[trade.symbol] = price
        self._spike_ref[trade.symbol] = price
        self.last_trade[trade.symbol] = trade
        self.trades += 1
        self.mark_received(trade.symbol, trade.ts)
        return True

    def on_book(self, book: OrderBook) -> None:
        if book.symbol not in self._known:
            return
        self.books[book.symbol] = book
        self.mark_received(book.symbol, book.ts)

    def _quarantine(self, trade: Trade, reason: str, reference: Decimal | None = None) -> None:
        self.spikes += 1
        self.quarantined.append(
            {
                "symbol": trade.symbol,
                "ts": iso(trade.ts),
                "price": to_str(trade.price),
                "reference": to_str(reference) if reference is not None else None,
                "reason": reason,
            }
        )

    # ------------------------------------------------------------------
    # bars
    # ------------------------------------------------------------------
    def on_base_bar(self, candle: Candle) -> list[Candle]:
        """Store a base bar and return the base bar plus any higher-timeframe close.

        The base bar is always returned (even when it is not in ``timeframes``)
        so the engine sees one event per market minute and can snapshot equity,
        refresh freshness and drive time-based logic on that cadence.
        """
        if candle.timeframe != self.base_timeframe:
            raise ValueError(
                f"expected a {self.base_timeframe} bar, got {candle.timeframe} for {candle.symbol}"
            )
        if candle.symbol not in self._known:
            return []
        base = self.series[(candle.symbol, self.base_timeframe)]
        if not base.append(candle):
            if base.last_ts is not None and utc(candle.ts) == utc(base.last_ts):
                self.duplicates += 1
            else:
                self.out_of_order += 1
            return []
        self.bars += 1
        # A closed bar is a price observation: on a bars-only feed (replay) it is the
        # only mark there is, and on a live feed its close is the newest print.
        self.last_price[candle.symbol] = candle.close
        closed = self.aggregators[candle.symbol].on_bar(candle)
        for bar in closed:
            series = self.series.get((bar.symbol, bar.timeframe))
            if series is not None:
                series.append(bar)
        return [candle, *closed]

    def candles(self, symbol: str, timeframe: str) -> CandleSeries:
        """The series for this pair; an empty series when nothing has arrived yet."""
        series = self.series.get((symbol, timeframe))
        if series is None:
            series = CandleSeries(symbol, timeframe, self.series_maxlen)
            self.series[(symbol, timeframe)] = series
        return series

    # ------------------------------------------------------------------
    # prices and books
    # ------------------------------------------------------------------
    def price(self, symbol: str) -> Decimal | None:
        """Last trade (never a quarantined one), else the book mid, else last close."""
        price = self.last_price.get(symbol)
        if price is not None:
            return price
        book = self.books.get(symbol)
        if book is not None and book.mid is not None:
            return book.mid
        series = self.series.get((symbol, self.base_timeframe))
        if series is not None and len(series):
            return series.last_close
        return None

    def book(self, symbol: str) -> OrderBook | None:
        return self.books.get(symbol)

    def bid_ask(self, symbol: str) -> tuple[Decimal | None, Decimal | None]:
        book = self.books.get(symbol)
        if book is None:
            return None, None
        return book.best_bid, book.best_ask

    def spread_pct(self, symbol: str) -> Decimal:
        book = self.books.get(symbol)
        return book.spread_pct if book is not None else ZERO

    # ------------------------------------------------------------------
    # freshness
    # ------------------------------------------------------------------
    def mark_received(self, symbol: str, ts: datetime | None = None) -> None:
        self.received[symbol] = utc(ts) if ts is not None else utcnow()

    def age_seconds(self, symbol: str, now: datetime | None = None) -> float:
        """Seconds since the last update. ``NEVER_RECEIVED_SECONDS`` when unknown.

        A finite sentinel (not ``inf``) keeps the value JSON-safe for the dashboard.
        """
        received = self.received.get(symbol)
        if received is None:
            return NEVER_RECEIVED_SECONDS
        now = utc(now) if now is not None else received
        return max((now - received).total_seconds(), 0.0)

    def is_stale(self, symbol: str, now: datetime, max_age: float) -> bool:
        return self.age_seconds(symbol, now) > max_age

    def is_fresh(self, symbol: str, now: datetime, max_age: float) -> bool:
        """True when this symbol delivered an update within ``max_age`` seconds."""
        return not self.is_stale(symbol, now, max_age)

    def stale_symbols(self, now: datetime, max_age: float) -> list[str]:
        return [symbol for symbol in self.symbols if self.age_seconds(symbol, now) > max_age]

    def all_stale(self, now: datetime, max_age: float) -> bool:
        if not self.symbols:
            return False
        return all(self.age_seconds(symbol, now) > max_age for symbol in self.symbols)

    def fresh_symbols(self, now: datetime, max_age: float) -> list[str]:
        return [symbol for symbol in self.symbols if self.age_seconds(symbol, now) <= max_age]

    def gap_symbols(self, now: datetime, timeframe: str, max_gap_bars: int = 2) -> list[str]:
        """Symbols whose last closed bar is more than ``max_gap_bars`` old (continuity)."""
        seconds = timeframe_seconds(timeframe)
        expected = floor_ts(now, seconds)
        out: list[str] = []
        for symbol in self.symbols:
            series = self.series.get((symbol, timeframe))
            if series is None or series.last_ts is None:
                out.append(symbol)
                continue
            if expected - utc(series.last_ts) > timedelta(seconds=seconds * max_gap_bars):
                out.append(symbol)
        return out

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        return {
            "symbols": {
                symbol: {
                    "price": to_str(price) if (price := self.price(symbol)) is not None else None,
                    "age_seconds": round(self.age_seconds(symbol), 1),
                    "book": symbol in self.books,
                    "bars": {
                        tf: len(series)
                        for (sym, tf), series in self.series.items()
                        if sym == symbol
                    },
                }
                for symbol in self.symbols
            },
            "quarantined": len(self.quarantined),
            "spikes": self.spikes,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "trades": self.trades,
            "bars": self.bars,
            "last_quarantine": dict(self.quarantined[-1]) if self.quarantined else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MarketData symbols={len(self.symbols)} series={len(self.series)} trades={self.trades}>"


__all__ = ["NEVER_RECEIVED_SECONDS", "MarketData", "TimeframeAggregator", "gap_report"]
