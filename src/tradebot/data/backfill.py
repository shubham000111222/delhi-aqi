"""Historical backfill: paginate a REST candle endpoint into the archive.

The streaming feed covers the last few minutes and a reconnect gap. It cannot cover three
months of history, which is what the strategies actually need before any of their results
mean anything. This is that path.

Everything here is written around the ways a paginated backfill goes wrong:

* **It never advances.** An exchange that ignores `since` returns the same page forever.
  The cursor check stops that, and the report says `stuck` rather than quietly looping.
* **It repeats rows.** Pages overlap at the boundary; a dict keyed on the bar's open time
  makes the result set idempotent, and the duplicate count is reported.
* **It invents a continuous series.** A dropped page leaves a hole that every indicator
  downstream will happily compute across. Gaps are detected against the timeframe and
  listed, so the archive can be topped up instead of trusted blindly.
* **It returns the bar that has not closed yet.** The current candle is dropped: a
  partial bar is a future leak waiting to happen.

The fetch itself is injected, so the pagination logic is tested without an exchange.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..core.clock import timeframe_seconds
from ..core.models import Candle

#: ``fetcher(symbol, timeframe, since, until, limit)`` returns bars in any order.
Fetcher = Callable[[str, str, datetime, datetime, int], Awaitable[Sequence[Candle]]]


@dataclass
class BackfillReport:
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    pages: int = 0
    fetched: int = 0
    kept: int = 0
    duplicates: int = 0
    dropped_out_of_range: int = 0
    dropped_unclosed: int = 0
    gaps: list[tuple[datetime, int]] = field(default_factory=list)
    stuck: bool = False
    complete: bool = False
    error: str | None = None

    @property
    def expected_bars(self) -> int:
        if self.kept == 0:
            return 0
        step = timedelta(seconds=timeframe_seconds(self.timeframe))
        span = self.end - self.start
        return max(0, int(span / step))

    @property
    def coverage(self) -> float:
        expected = self.expected_bars
        return 0.0 if expected == 0 else min(1.0, self.kept / expected)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "pages": self.pages,
            "fetched": self.fetched,
            "kept": self.kept,
            "duplicates": self.duplicates,
            "dropped_out_of_range": self.dropped_out_of_range,
            "dropped_unclosed": self.dropped_unclosed,
            "gaps": [{"after": ts.isoformat(), "missing": count} for ts, count in self.gaps],
            "expected_bars": self.expected_bars,
            "coverage": round(self.coverage, 4),
            "stuck": self.stuck,
            "complete": self.complete,
            "error": self.error,
        }


class Backfill:
    """Paginate a fetcher into one de-duplicated, gap-checked series."""

    def __init__(self, fetcher: Fetcher, *, page_limit: int = 500, max_pages: int = 500) -> None:
        self.fetcher = fetcher
        self.page_limit = max(1, int(page_limit))
        self.max_pages = max(1, int(max_pages))

    async def run(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        *,
        now: datetime | None = None,
    ) -> tuple[list[Candle], BackfillReport]:
        start, end = _utc(start), _utc(end)
        if end <= start:
            raise ValueError("the backfill window must end after it starts")
        step = timedelta(seconds=timeframe_seconds(timeframe))
        moment = _utc(now or datetime.now(tz=UTC))
        report = BackfillReport(symbol=symbol, timeframe=timeframe, start=start, end=end)
        bars: dict[datetime, Candle] = {}
        cursor = start
        last_cursor: datetime | None = None

        while report.pages < self.max_pages and cursor < end:
            if last_cursor is not None and cursor <= last_cursor:
                # The exchange did not move: without this the loop never ends.
                report.stuck = True
                break
            last_cursor = cursor
            report.pages += 1
            try:
                page = await self.fetcher(symbol, timeframe, cursor, end, self.page_limit)
            except Exception as exc:  # noqa: BLE001 - report the fault, keep what was fetched
                report.error = f"{type(exc).__name__}: {exc}"
                break
            page = list(page or [])
            report.fetched += len(page)
            if not page:
                report.complete = True
                break
            newest = cursor
            for candle in page:
                ts = _utc(candle.ts)
                if ts < start or ts >= end:
                    report.dropped_out_of_range += 1
                    continue
                if ts + step > moment:
                    report.dropped_unclosed += 1
                    continue
                if ts in bars:
                    report.duplicates += 1
                    continue
                bars[ts] = candle
                newest = max(newest, ts)
            if newest <= cursor:
                report.stuck = True
                break
            cursor = newest + step

        ordered = [bars[ts] for ts in sorted(bars)]
        report.kept = len(ordered)
        report.gaps = _find_gaps(ordered, step)
        report.complete = report.complete or (bool(ordered) and ordered[-1].ts + step >= end)
        return ordered, report


def _find_gaps(candles: Sequence[Candle], step: timedelta) -> list[tuple[datetime, int]]:
    """Missing bars as ``(bar_after_which_they_are_missing, count)``."""
    gaps: list[tuple[datetime, int]] = []
    for previous, current in zip(candles, candles[1:], strict=False):
        delta = current.ts - previous.ts
        if delta > step:
            missing = int(delta / step) - 1
            if missing > 0:
                gaps.append((previous.ts, missing))
    return gaps


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


@dataclass
class RestFetcher:
    """A ccxt REST fetcher: one `fetch_ohlcv` page per call, exchange held open."""

    config: Any
    state: dict[str, Any] = field(default_factory=dict)

    async def _exchange(self) -> Any:
        exchange = self.state.get("exchange")
        if exchange is None:
            import ccxt.async_support as ccxt  # lazy: nothing else needs it

            exchange = getattr(ccxt, self.config.settings.ccxt.exchange_id)(
                {"enableRateLimit": True, "options": {"defaultType": "spot"}}
            )
            # Stored before `load_markets`: if that call fails, the session still exists
            # and has to be closed, or aiohttp complains and a socket leaks.
            self.state["exchange"] = exchange
            await exchange.load_markets()
        return exchange

    async def __call__(
        self, symbol: str, timeframe: str, since: datetime, until: datetime, limit: int
    ) -> Sequence[Candle]:
        exchange = await self._exchange()
        raw = await exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, since=int(since.timestamp() * 1000), limit=limit
        )
        out: list[Candle] = []
        for row in raw or []:
            ts = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
            if ts >= until:
                continue
            out.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    ts=ts,
                    open=_decimal(row[1]),
                    high=_decimal(row[2]),
                    low=_decimal(row[3]),
                    close=_decimal(row[4]),
                    volume=_decimal(row[5]),
                )
            )
        return out

    async def aclose(self) -> None:
        """Release the ccxt session; leaving it open logs an unclosed-session error."""
        exchange = self.state.pop("exchange", None)
        if exchange is not None:
            with contextlib.suppress(Exception):  # shutdown must not raise
                await exchange.close()


def rest_fetcher(config: Any) -> RestFetcher:
    return RestFetcher(config=config)


def _decimal(value: Any) -> Any:
    from decimal import Decimal

    return Decimal(str(value))


def synthetic_fetcher(
    *, seed: int = 7, page_size: int = 240, drift_bars: int = 0
) -> Fetcher:
    """A deterministic stand-in for an exchange, used to exercise pagination offline.

    It behaves like a real endpoint: it answers `since`/`limit` pages, it can be told to
    skip a block of bars (`drift_bars`) so gap detection has something to find, and it
    returns rows in exchange order.
    """
    import math
    import random

    #: One slow cycle, so a demo backfill run in 2031 looks like one run in 2024. Using
    #: the absolute epoch minute instead put prices at exp(598) ~ 1e259 for any modern
    #: date - the pagination was still honest, but the prices were unusable.
    CYCLE = 43_200  # 30 days of 1m bars

    def price_at(index: int) -> float:
        phase = 2 * math.pi * (index % CYCLE) / CYCLE
        rng = random.Random(seed * 1_000_003 + (index % CYCLE) // 13)
        drift = 0.15 * math.sin(phase)  # continuous across the wrap, unlike `index * k`
        return 100.0 * math.exp(drift + 0.004 * rng.gauss(0.0, 1.0))

    async def fetch(
        symbol: str, timeframe: str, since: datetime, until: datetime, limit: int
    ) -> Sequence[Candle]:
        from decimal import Decimal

        step = timeframe_seconds(timeframe)
        start_index = int(since.timestamp()) // step
        end_index = int(until.timestamp()) // step
        out: list[Candle] = []
        for index in range(start_index, min(end_index, start_index + min(limit, page_size))):
            if drift_bars and index % (drift_bars + 7) >= 7:
                continue  # a hole in the series, like a dropped page
            ts = datetime.fromtimestamp(index * step, tz=UTC)
            price = price_at(index)
            out.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    ts=ts,
                    open=Decimal(f"{price:.4f}"),
                    high=Decimal(f"{price * 1.001:.4f}"),
                    low=Decimal(f"{price * 0.999:.4f}"),
                    close=Decimal(f"{price * 1.0005:.4f}"),
                    volume=Decimal("12.5"),
                )
            )
        return out

    return fetch
