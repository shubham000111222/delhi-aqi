"""Pagination, de-duplication and gap detection for the historical backfill."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tradebot.core.models import Candle
from tradebot.data.backfill import Backfill, synthetic_fetcher

START = datetime(2024, 1, 1, tzinfo=UTC)
NOW = datetime(2024, 2, 1, tzinfo=UTC)


def bar(ts: datetime, close: float = 100.0) -> Candle:
    return Candle(
        symbol="BTC/USDT",
        timeframe="1m",
        ts=ts,
        open=Decimal(str(close)),
        high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)),
        close=Decimal(str(close)),
        volume=Decimal("1"),
    )


def series(start: datetime, count: int, *, step_minutes: int = 1) -> list[Candle]:
    return [bar(start + timedelta(minutes=step_minutes * i), 100 + i * 0.1) for i in range(count)]


def paged(bars: list[Candle], *, overlap: int = 0, page_size: int = 10) -> Any:
    """A fetcher that answers `since`/`limit` pages, optionally overlapping them."""
    calls: list[datetime] = []

    async def fetch(symbol: str, timeframe: str, since: datetime, until: datetime, limit: int) -> list[Candle]:
        calls.append(since)
        candidates = [b for b in bars if b.ts >= since and b.ts < until]
        size = min(page_size, limit)
        if overlap and calls:
            start = max(0, len([b for b in bars if b.ts < since]) - overlap)
            candidates = bars[start : start + size]
        return candidates[:size]

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


class TestPagination:
    def test_it_walks_pages_until_the_window_is_covered(self):
        bars = series(START, 30)
        fetch = paged(bars, page_size=10)
        backfill = Backfill(fetch)

        async def run():
            return await backfill.run("BTC/USDT", "1m", START, START + timedelta(minutes=30), now=NOW)

        got, report = asyncio.run(run())
        assert len(got) == 30
        assert [c.ts for c in got] == [c.ts for c in bars]
        assert report.pages == 3
        assert report.complete is True and report.stuck is False
        assert report.gaps == []

    def test_an_overlapping_exchange_does_not_duplicate_rows(self):
        bars = series(START, 30)
        fetch = paged(bars, overlap=3, page_size=10)
        backfill = Backfill(fetch)

        async def run():
            return await backfill.run("BTC/USDT", "1m", START, START + timedelta(minutes=30), now=NOW)

        got, report = asyncio.run(run())
        assert [c.ts for c in got] == [c.ts for c in bars]
        assert report.duplicates > 0
        assert report.kept == 30

    def test_a_stubborn_exchange_stops_instead_of_looping(self):
        bars = series(START, 5)

        async def fetch(symbol, timeframe, since, until, limit):  # noqa: ARG001
            return bars[:2]  # always the same two rows, however far along we are

        async def run():
            return await Backfill(fetch).run("BTC/USDT", "1m", START, START + timedelta(minutes=60), now=NOW)

        got, report = asyncio.run(run())
        assert report.stuck is True  # it never advanced past the first page
        assert report.pages == 2, "one page that did not move is enough to stop"
        assert report.kept == 2

    def test_the_page_cap_is_reported_not_hidden(self):
        bars = series(START, 100)
        backfill = Backfill(paged(bars, page_size=5), max_pages=3)

        async def run():
            return await backfill.run("BTC/USDT", "1m", START, START + timedelta(minutes=100), now=NOW)

        _got, report = asyncio.run(run())
        assert report.pages == 3
        assert report.complete is False, "a truncated backfill must not claim to be complete"

    def test_an_exchange_error_keeps_what_was_already_fetched(self):
        bars = series(START, 30)
        calls = {"n": 0}

        async def fetch(symbol, timeframe, since, until, limit):  # noqa: ARG001
            calls["n"] += 1
            if calls["n"] == 3:
                raise ConnectionError("rate limited")
            return [b for b in bars if b.ts >= since][:10]

        async def run():
            return await Backfill(fetch).run("BTC/USDT", "1m", START, START + timedelta(minutes=30), now=NOW)

        got, report = asyncio.run(run())
        assert report.error is not None and "ConnectionError" in report.error
        assert len(got) == 20, "the first two pages are still worth keeping"
        assert report.complete is False


class TestFidelity:
    def test_out_of_range_rows_are_dropped(self):
        inside = series(START, 5)
        noise = [bar(START - timedelta(minutes=5)), bar(START + timedelta(minutes=99))]

        async def fetch(symbol, timeframe, since, until, limit):  # noqa: ARG001
            return inside + noise

        async def run():
            return await Backfill(fetch).run("BTC/USDT", "1m", START, START + timedelta(minutes=10), now=NOW)

        got, report = asyncio.run(run())
        assert len(got) == 5
        assert report.dropped_out_of_range >= 2, "the same page may be fetched more than once"

    def test_the_unclosed_bar_is_dropped(self):
        moment = START + timedelta(minutes=3, seconds=30)
        bars = series(START, 5)

        async def fetch(symbol, timeframe, since, until, limit):  # noqa: ARG001
            return bars

        async def run():
            return await Backfill(fetch).run("BTC/USDT", "1m", START, START + timedelta(minutes=5), now=moment)

        got, report = asyncio.run(run())
        assert all(c.ts + timedelta(minutes=1) <= moment for c in got)
        assert len(got) == 3, "the bar still forming at `now` is a future leak"
        assert report.dropped_unclosed >= 2, "counted once per page that returns them"

    def test_unsorted_pages_come_out_in_order(self):
        bars = series(START, 6)

        async def fetch(symbol, timeframe, since, until, limit):  # noqa: ARG001
            return list(reversed(bars))

        async def run():
            return await Backfill(fetch).run("BTC/USDT", "1m", START, START + timedelta(minutes=6), now=NOW)

        got, _report = asyncio.run(run())
        assert [c.ts for c in got] == sorted(c.ts for c in bars)

    def test_a_hole_in_the_series_is_reported_with_its_size(self):
        bars = series(START, 10)
        holed = bars[:4] + bars[7:]  # three minutes missing after 00:03

        async def fetch(symbol, timeframe, since, until, limit):  # noqa: ARG001
            return holed

        async def run():
            return await Backfill(fetch).run("BTC/USDT", "1m", START, START + timedelta(minutes=10), now=NOW)

        got, report = asyncio.run(run())
        assert len(got) == 7
        assert report.gaps == [(START + timedelta(minutes=3), 3)]
        assert report.coverage < 1.0

    def test_a_window_that_ends_before_it_starts_is_a_bug_not_a_no_op(self):
        async def fetch(*args, **kwargs):  # noqa: ARG001
            return []

        with pytest.raises(ValueError, match="must end after"):
            asyncio.run(Backfill(fetch).run("BTC/USDT", "1m", START, START))


class TestSyntheticFetcher:
    def test_the_offline_exchange_paginates_like_a_real_one(self):
        fetch = synthetic_fetcher(seed=3, page_size=120)
        backfill = Backfill(fetch, page_limit=120)

        async def run():
            return await backfill.run("BTC/USDT", "1m", START, START + timedelta(hours=6), now=NOW)

        got, report = asyncio.run(run())
        assert len(got) == 360
        assert report.pages == 3
        assert report.stuck is False and report.error is None
        assert report.gaps == []

    def test_it_can_be_told_to_leave_a_hole(self):
        fetch = synthetic_fetcher(seed=3, page_size=60, drift_bars=5)
        backfill = Backfill(fetch, page_limit=60)

        async def run():
            return await backfill.run("BTC/USDT", "1m", START, START + timedelta(hours=2), now=NOW)

        _got, report = asyncio.run(run())
        assert report.gaps, "the synthetic exchange's holes must be visible in the report"


class TestTheTail:
    """The right edge of a real backfill: the bar that has not closed yet.

    A live exchange answers with the forming candle, so the last page of a long fetch
    contains a row the loop must drop. That page then moves no cursor, which used to be
    reported as `stuck` - "the exchange is repeating itself" - on a backfill that was in
    fact perfect. These are the numbers a real 30-day Binance backfill produced.
    """

    def _exchange(self, moment: datetime):
        calls = {"n": 0}

        async def fetch(symbol, timeframe, since, until, limit):  # noqa: ARG001
            calls["n"] += 1
            first = since.replace(second=0, microsecond=0)
            ts = first if first >= since else first + timedelta(minutes=1)
            out = []
            while len(out) < limit and ts < until and ts <= moment.replace(second=0, microsecond=0):
                out.append(bar(ts))
                ts += timedelta(minutes=1)
            return out

        return fetch

    def test_an_unclosed_tail_is_complete_not_stuck(self):
        end = datetime(2026, 11, 3, 21, 52, 5, tzinfo=UTC)
        start = end - timedelta(days=30)

        async def run():
            return await Backfill(self._exchange(end), page_limit=1000).run(
                "BTC/USDT", "1m", start, end, now=end
            )

        got, report = asyncio.run(run())
        assert report.stuck is False, "the exchange did not repeat itself; the last bar was open"
        assert report.complete is True
        assert report.dropped_unclosed == 2, "counted once per page that returns the open bar"
        assert report.gaps == []
        assert report.expected_bars == report.kept, "expected must exclude the bar that had not closed"
        assert report.coverage == 1.0
        assert got[-1].ts + timedelta(minutes=1) <= end

    def test_a_page_of_closed_rows_that_does_not_advance_is_still_stuck(self):
        bars = series(START, 3)

        async def fetch(symbol, timeframe, since, until, limit):  # noqa: ARG001
            return [bars[0]]  # one closed row, forever: the shape `stuck` exists for

        async def run():
            return await Backfill(fetch).run(
                "BTC/USDT", "1m", START, START + timedelta(minutes=30), now=NOW
            )

        _got, report = asyncio.run(run())
        assert report.stuck is True
        assert report.complete is False

    def test_a_missing_bar_in_the_middle_is_not_complete(self):
        bars = series(START, 10)
        holed = bars[:4] + bars[7:]

        async def fetch(symbol, timeframe, since, until, limit):  # noqa: ARG001
            return holed

        async def run():
            return await Backfill(fetch).run(
                "BTC/USDT", "1m", START, START + timedelta(minutes=10), now=NOW
            )

        _got, report = asyncio.run(run())
        assert report.expected_bars == 10
        assert report.kept == 7
        assert report.complete is False
        assert round(report.coverage, 4) == 0.7
