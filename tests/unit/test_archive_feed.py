"""Tests for replaying a recorded archive: the bridge from `backfill` to `backtest`.

The archive is only useful if a backtest can read it back, start where the data starts,
and see a book it can fill against. Each of those was a real bug once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pyarrow", reason='archive tests need pip install -e ".[data]"')
pytest.importorskip("duckdb", reason='archive tests need pip install -e ".[data]"')

from tradebot.config import load_config  # noqa: E402
from tradebot.core.clock import SimClock  # noqa: E402
from tradebot.core.models import Candle  # noqa: E402
from tradebot.core.money import D  # noqa: E402
from tradebot.data import archive as archive_mod  # noqa: E402
from tradebot.data.archive import Archive, read_candles  # noqa: E402
from tradebot.data.feed import ReplayFeed, make_feed  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def archive_cfg(tmp_path: Path):
    """A config whose archive lives in tmp_path, so nothing touches the real one."""
    config = load_config(
        ROOT / "config",
        environ={},
        overrides={
            "data_dir": str(tmp_path / "data"),
            "report": {"dir": str(tmp_path / "reports")},
            "feed": "archive",
        },
    )
    config.ensure_dirs()
    archive_mod._CANDLE_CACHE.clear()
    yield config
    archive_mod._CANDLE_CACHE.clear()


def bars(count: int, *, start: datetime, symbol: str = "BTC/USDT", price: str = "100") -> list[Candle]:
    base = D(price)
    return [
        Candle(
            symbol=symbol,
            timeframe="1m",
            ts=start + timedelta(minutes=i),
            open=base,
            high=base * D("1.001"),
            low=base * D("0.999"),
            close=base * D("1.0002"),
            volume=D("10"),
        )
        for i in range(count)
    ]


class TestReadCandles:
    def test_round_trips_what_backfill_wrote(self, archive_cfg):
        start = datetime(2026, 3, 2, tzinfo=UTC)
        written = bars(30, start=start)
        Archive(archive_cfg.settings.archive_dir).write_candles(written)

        loaded = read_candles(archive_cfg)

        assert set(loaded) == {"BTC/USDT"}  # only symbols the archive actually holds
        btc = loaded["BTC/USDT"]
        assert len(btc) == 30
        assert btc[0].ts == start
        assert btc[0].close == D("100.02")
        assert [bar.ts for bar in btc] == sorted(bar.ts for bar in btc)

    def test_an_empty_archive_says_so_in_plain_words(self, archive_cfg):
        with pytest.raises(ValueError) as exc:
            read_candles(archive_cfg)
        assert "backfill" in str(exc.value)

    def test_repeated_reads_do_not_re_read_the_files(self, archive_cfg, monkeypatch):
        Archive(archive_cfg.settings.archive_dir).write_candles(
            bars(5, start=datetime(2026, 3, 2, tzinfo=UTC))
        )
        calls = {"n": 0}
        real = Archive.candles

        def counting(self, *args, **kwargs):
            calls["n"] += 1
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Archive, "candles", counting)
        first = read_candles(archive_cfg)
        second = read_candles(archive_cfg)
        assert first is second
        assert calls["n"] < 20  # one query per symbol, not per engine build


class TestMakeFeed:
    def test_archive_builds_a_replay_feed_with_a_sim_clock(self, archive_cfg):
        Archive(archive_cfg.settings.archive_dir).write_candles(
            bars(10, start=datetime(2026, 3, 2, tzinfo=UTC))
        )
        clock = SimClock(datetime(2026, 3, 2, tzinfo=UTC))

        feed = make_feed(archive_cfg, clock=clock)

        assert isinstance(feed, ReplayFeed)
        assert feed.describe()["prices"] == "recorded"
        assert feed.describe()["depth"].startswith("modelled")

    def test_a_live_feed_is_refused_on_a_simulated_clock(self, archive_cfg):
        clock = SimClock(datetime(2026, 3, 2, tzinfo=UTC))
        for kind in ("ccxt", "stream"):
            live_cfg = load_config(
                ROOT / "config",
                environ={},
                overrides={
                    "data_dir": str(archive_cfg.settings.data_dir),
                    "feed": kind,
                },
            )
            with pytest.raises(ValueError) as exc:
                make_feed(live_cfg, clock=clock)
            assert "clock" in str(exc.value).lower()


class TestReplayFeed:
    def test_a_replay_emits_a_book_so_orders_can_fill(self, archive_cfg):
        import asyncio

        start = datetime(2026, 3, 2, tzinfo=UTC)
        feed = ReplayFeed(
            archive_cfg,
            {"BTC/USDT": bars(3, start=start)},
            SimClock(start),
        )
        seen: list[str] = []
        books = []

        async def emit(kind, payload):
            seen.append(kind)
            if kind == "book":
                books.append(payload)

        asyncio.run(feed.run(emit))

        assert seen.count("bar") == 3
        assert seen.count("book") == 3
        top = books[0]
        assert top.bids and top.asks
        assert top.bids[0].price < top.asks[0].price  # a book, not a single price
        assert top.bids[0].qty > 0
        # deterministic: the same tape always yields the same book
        assert feed._book(bars(1, start=start)[0]).bids[0].price == top.bids[0].price

    def test_books_can_be_switched_off(self, archive_cfg):
        import asyncio

        start = datetime(2026, 3, 2, tzinfo=UTC)
        feed = ReplayFeed(archive_cfg, {"BTC/USDT": bars(2, start=start)}, SimClock(start), books=False)
        seen: list[str] = []

        async def emit(kind, payload):
            seen.append(kind)

        asyncio.run(feed.run(emit))
        assert seen == ["bar", "bar"]
        assert feed.describe()["depth"] == "absent"


class TestDemoPriceGenerator:
    def test_demo_prices_stay_near_100_whatever_the_date(self):
        """The demo's drift used to be `index * k` on the absolute epoch minute: at a
        2026 start that is exp(598), which is not a demo, it is a numeric explosion."""
        import asyncio

        from tradebot.data.backfill import synthetic_fetcher

        fetch = synthetic_fetcher()
        for year in (2024, 2026, 2031):
            since = datetime(year, 5, 5, tzinfo=UTC)
            rows = asyncio.run(
                fetch("BTC/USDT", "1m", since, since + timedelta(minutes=200), 200)
            )
            prices = [float(row.close) for row in rows]
            assert prices, "the demo must return rows"
            assert all(50.0 < price < 200.0 for price in prices), (year, min(prices), max(prices))

    def test_demo_prices_are_continuous_across_the_cycle_wrap(self):
        import asyncio

        from tradebot.data.backfill import synthetic_fetcher

        fetch = synthetic_fetcher()
        since = datetime(2026, 3, 2, tzinfo=UTC)
        rows = asyncio.run(fetch("BTC/USDT", "1m", since, since + timedelta(minutes=3), 200))
        closes = [float(row.close) for row in rows]
        for a, b in zip(closes, closes[1:], strict=False):
            assert abs(b - a) / a < 0.02
