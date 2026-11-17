from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.analysis.archive_history import ArchiveHistoryProvider
from tradebot.core.models import Candle
from tradebot.data.archive import Archive

END = datetime(2024, 7, 1, tzinfo=UTC)


def bar(ts, *, symbol='BTC/USDT', timeframe='5m'):
    return Candle(symbol, timeframe, ts, Decimal('100.000000000000000001'),
                  Decimal('101'), Decimal('99'), Decimal('100.1'), Decimal('1'))


def test_archive_cutoff_exact_values_symbol_and_timeframe(tmp_path):
    archive = Archive(tmp_path / "operator's archive")
    closed = bar(END - timedelta(minutes=5))
    archive.write_candles([closed, bar(END), bar(END - timedelta(minutes=4)),
                           bar(END - timedelta(days=61)),
                           bar(END - timedelta(minutes=5), symbol='ETH/USDT'),
                           bar(END - timedelta(minutes=5), timeframe='1m')])
    provider = ArchiveHistoryProvider(archive)
    assert provider('BTC/USDT', END) == (closed,)
    assert provider("BTC/USDT' OR true --", END) == ()
    archive.write_candles([bar(END + timedelta(days=1))])
    assert provider('BTC/USDT', END) == (closed,)


def test_missing_archive_and_naive_cutoff_refused(tmp_path):
    provider = ArchiveHistoryProvider(Archive(tmp_path))
    with pytest.raises(ValueError, match='timezone'):
        provider('BTC/USDT', END.replace(tzinfo=None))
    with pytest.raises(ValueError, match='no recorded'):
        provider('BTC/USDT', END)


@pytest.mark.parametrize('days', [True, 29, 181, 60.5])
def test_invalid_lookback(tmp_path, days):
    with pytest.raises(ValueError):
        ArchiveHistoryProvider(Archive(tmp_path), days=days)


def test_duplicate_and_gap_are_not_fabricated_away(tmp_path):
    archive = Archive(tmp_path)
    a = bar(END - timedelta(minutes=15))
    b = bar(END - timedelta(minutes=5))
    archive.write_candles([a, a, b])
    assert ArchiveHistoryProvider(archive)('BTC/USDT', END) == (a, a, b)


def test_future_invalid_values_are_not_loaded(tmp_path):
    archive = Archive(tmp_path)
    closed = bar(END - timedelta(minutes=5))
    archive.write_candles([closed, replace(bar(END), close=Decimal('NaN'))])
    assert ArchiveHistoryProvider(archive)('BTC/USDT', END) == (closed,)
