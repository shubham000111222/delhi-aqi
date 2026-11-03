"""Tests for the Parquet/DuckDB archive.

Skipped when the optional `data` extra is not installed: the trading engine must not
depend on it, so the suite must not either.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

pytest.importorskip("pyarrow", reason='archive tests need pip install -e ".[data]"')
pytest.importorskip("duckdb", reason='archive tests need pip install -e ".[data]"')

from tradebot.core.models import BookLevel, Candle, OrderBook  # noqa: E402
from tradebot.core.money import D  # noqa: E402
from tradebot.data.archive import Archive, write_tape_to_archive  # noqa: E402

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def candles(count: int, *, symbol: str = "BTC/USDT", start: datetime = T0) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            timeframe="1m",
            ts=start + timedelta(minutes=i),
            open=D("100"),
            high=D("101"),
            low=D("99"),
            close=D("100.5"),
            volume=D("10"),
        )
        for i in range(count)
    ]


def books(count: int, *, symbol: str = "BTC/USDT", spread: str = "0.1") -> list[OrderBook]:
    return [
        OrderBook(
            symbol=symbol,
            ts=T0 + timedelta(minutes=i),
            bids=(BookLevel(price=D("100.0"), qty=D("1.5")),),
            asks=(BookLevel(price=D("100.0") + D(spread), qty=D("2.0")),),
        )
        for i in range(count)
    ]


class TestArchiveWrites:
    def test_candles_are_partitioned_by_symbol_and_day(self, tmp_path):
        archive = Archive(tmp_path)
        archive.write_candles(candles(5, symbol="BTC/USDT") + candles(3, symbol="ETH/USDT"))
        assert (tmp_path / "candles" / "sym=BTC-USDT" / "day=2024-01-01" / "candles.parquet").exists()
        assert (tmp_path / "candles" / "sym=ETH-USDT" / "day=2024-01-01" / "candles.parquet").exists()

    def test_queries_return_the_written_rows(self, tmp_path):
        archive = Archive(tmp_path)
        archive.write_candles(candles(10))
        rows = archive.candles(symbol="BTC/USDT", limit=100)
        assert len(rows) == 10
        assert rows[0]["symbol"] == "BTC/USDT"
        assert rows[0]["close"] == "100.5"  # decimals stored as strings, not floats

    def test_rewriting_the_same_data_is_idempotent(self, tmp_path):
        archive = Archive(tmp_path)
        archive.write_candles(candles(10))
        archive.write_candles(candles(10))
        assert len(archive.candles(symbol="BTC/USDT", limit=100)) == 10

    def test_new_bars_are_appended(self, tmp_path):
        archive = Archive(tmp_path)
        archive.write_candles(candles(5))
        archive.write_candles(candles(5, start=T0 + timedelta(minutes=5)))
        assert len(archive.candles(symbol="BTC/USDT", limit=100)) == 10

    def test_books_keep_spread_statistics(self, tmp_path):
        archive = Archive(tmp_path)
        archive.write_books(books(20, spread="0.1"))
        stats = archive.spread_stats(symbol="BTC/USDT")
        assert stats["observations"] == 20
        # spread 0.1 on a 100.0 bid = 10 bps
        assert float(stats["mean_spread_bps"]) == pytest.approx(10.0, rel=1e-6)

    def test_coverage_reports_what_is_stored(self, tmp_path):
        archive = Archive(tmp_path)
        archive.write_candles(candles(3, symbol="BTC/USDT") + candles(3, symbol="ETH/USDT"))
        coverage = archive.coverage()
        assert coverage["candles"]["files"] == 2
        assert coverage["candles"]["symbols"] == ["BTC-USDT", "ETH-USDT"]
        assert coverage["candles"]["days"] == 1

    def test_empty_input_writes_nothing(self, tmp_path):
        archive = Archive(tmp_path)
        assert archive.write_candles([]) == []
        assert archive.coverage()["candles"]["files"] == 0


class TestRetention:
    def test_old_partitions_are_removed(self, tmp_path):
        archive = Archive(tmp_path)
        old = candles(2, start=datetime(2023, 12, 1, tzinfo=UTC))
        recent = candles(2, start=datetime(2024, 1, 20, tzinfo=UTC))
        archive.write_candles(old + recent)
        removed = archive.apply_retention(keep_days=30, kind="candles", now=date(2024, 1, 21))
        assert len(removed) == 1
        remaining = archive.coverage()["candles"]["days"]
        assert remaining == 1

    def test_retention_keeps_recent_partitions(self, tmp_path):
        archive = Archive(tmp_path)
        archive.write_books(books(2))
        removed = archive.apply_retention(keep_days=90, kind="books", now=date(2024, 1, 2))
        assert removed == []


class TestTapeToArchive:
    def test_a_recorded_tape_becomes_candles_and_books(self, tmp_path):
        archive = Archive(tmp_path)
        events = [
            ("bar", candles(4)[i], T0) for i in range(4)
        ] + [("book", books(4)[i], T0) for i in range(4)] + [("tick", object(), T0)]
        stats = write_tape_to_archive(archive, events)  # type: ignore[arg-type]
        assert stats.candles == 4
        assert stats.books == 4
        assert stats.bytes > 0
        assert len(archive.candles(symbol="BTC/USDT", limit=10)) == 4

    def test_missing_optional_dependency_raises_a_clear_error(self, tmp_path, monkeypatch):
        from tradebot.data import archive as archive_module

        def boom(name: str):
            raise RuntimeError("the Parquet archive needs pyarrow")

        monkeypatch.setattr(archive_module, "_require", boom)
        archive = Archive(tmp_path)
        with pytest.raises(RuntimeError, match="needs pyarrow"):
            archive.write_candles(candles(1))
