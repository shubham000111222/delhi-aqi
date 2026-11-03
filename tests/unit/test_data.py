"""Indicators, candle series, timeframe aggregation and market data guards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from tests.conftest import make_candles

from tradebot.core.models import OrderBook, Trade
from tradebot.core.money import D
from tradebot.data.hub import MarketData, TimeframeAggregator, gap_report
from tradebot.data.series import CandleSeries
from tradebot.strategies import indicators as ta


class TestIndicators:
    def test_sma_matches_manual(self):
        x = np.array([1.0, 2, 3, 4, 5])
        out = ta.sma(x, 3)
        assert np.isnan(out[1])
        assert out[4] == pytest.approx(4.0)

    def test_ema_converges_on_constant_series(self):
        x = np.full(100, 7.0)
        out = ta.ema(x, 20)
        assert out[-1] == pytest.approx(7.0)

    def test_rsi_bounds_and_extremes(self):
        up = np.linspace(100, 200, 60)
        down = np.linspace(200, 100, 60)
        assert ta.last(ta.rsi(up, 14)) == pytest.approx(100.0)
        assert ta.last(ta.rsi(down, 14)) == pytest.approx(0.0, abs=1e-6)

    def test_atr_positive(self):
        c = np.linspace(100, 110, 50)
        value = ta.last(ta.atr(c + 1, c - 1, c, 14))
        assert value > 0

    def test_adx_trend_vs_range(self):
        trend = np.linspace(100, 140, 120)
        adx_trend, _, _ = ta.adx(trend + 0.5, trend - 0.5, trend, 14)
        flat = np.full(120, 100.0)
        adx_flat, _, _ = ta.adx(flat + 0.5, flat - 0.5, flat, 14)
        assert ta.last(adx_trend) > 40
        assert ta.last(adx_flat) < 5

    def test_donchian_excludes_current_bar(self):
        high = np.array([1.0, 2, 3, 4, 10])
        low = np.array([0.0, 0, 0, 0, 0])
        upper, lower = ta.donchian(high, low, 3)
        assert upper[4] == pytest.approx(4.0)  # not 10: the current bar is excluded
        assert lower[4] == pytest.approx(0.0)

    def test_bollinger_symmetric(self):
        c = np.array([1.0, 2, 3, 2, 1, 2, 3, 2, 1, 2])
        upper, mid, lower = ta.bollinger(c, 5, 2.0)
        assert upper[-1] - mid[-1] == pytest.approx(mid[-1] - lower[-1])

    def test_realized_vol_and_percentile(self):
        rng = np.random.default_rng(0)
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, 300)))
        rv = ta.realized_vol(c, 96)
        assert ta.last(rv) > 0
        pct = ta.percentile_rank(rv, 96)
        assert 0 <= ta.last(pct) <= 1


class TestCandleSeries:
    def test_rejects_duplicates_and_out_of_order(self, candles):
        series = CandleSeries("BTC/USDT", "1m", maxlen=10)
        assert series.append(candles[0]) is True
        assert series.append(candles[0]) is False
        assert series.append(candles[0].__class__(
            symbol="BTC/USDT", timeframe="1m",
            ts=candles[0].ts - timedelta(minutes=5),
            open=D(1), high=D(2), low=D(1), close=D(2), volume=D(1),
        )) is False
        assert len(series) == 1

    def test_ring_buffer_keeps_order(self):
        series = CandleSeries("BTC/USDT", "1m", maxlen=5)
        for c in make_candles(count=20):
            series.append(c)
        assert len(series) == 5
        closes = series.close
        assert closes[-1] == pytest.approx(60000 + 20 * 10)

    def test_wrong_symbol_rejected(self, candles):
        series = CandleSeries("ETH/USDT", "1m")
        with pytest.raises(ValueError):
            series.append(candles[0])

    def test_last_returns_candles(self):
        series = CandleSeries("BTC/USDT", "1m", maxlen=50)
        for c in make_candles(count=10):
            series.append(c)
        tail = series.last(3)
        assert len(tail) == 3
        assert tail[-1].ts > tail[0].ts


class TestAggregation:
    def test_15m_bars_close_on_boundary(self):
        agg = TimeframeAggregator("BTC/USDT", "1m", ["15m"])
        closed = []
        for c in make_candles(count=31):
            closed.extend(agg.on_bar(c))
        assert len(closed) == 2
        assert closed[0].timeframe == "15m"
        assert closed[0].ts == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        assert closed[1].ts == datetime(2024, 1, 1, 0, 15, tzinfo=UTC)

    def test_market_data_routes_and_dedupes(self):
        md = MarketData(["BTC/USDT"], "1m", ["15m"])
        bars = make_candles(count=20)
        emitted = []
        for c in bars:
            emitted.extend(md.on_base_bar(c))
        assert len([c for c in emitted if c.timeframe == "1m"]) == 20
        assert len([c for c in emitted if c.timeframe == "15m"]) == 1
        assert md.on_base_bar(bars[-1]) == []  # duplicate
        assert md.duplicates == 1
        assert len(md.candles("BTC/USDT", "15m")) == 1
        assert md.price("BTC/USDT") == bars[-1].close

    def test_spike_filter_quarantines(self):
        md = MarketData(["BTC/USDT"], "1m", ["15m"], spike_pct=D("0.05"))
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        assert md.on_trade(Trade("BTC/USDT", ts, D("100"), D("1"))) is True
        assert md.on_trade(Trade("BTC/USDT", ts + timedelta(seconds=1), D("120"), D("1"))) is False
        assert len(md.quarantined) == 1

    def test_freshness(self):
        md = MarketData(["BTC/USDT"], "1m", ["15m"])
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        md.on_trade(Trade("BTC/USDT", ts, D("100"), D("1")))
        assert md.is_fresh("BTC/USDT", ts + timedelta(seconds=5), 15)
        assert not md.is_fresh("BTC/USDT", ts + timedelta(seconds=30), 15)
        assert md.all_stale(ts + timedelta(seconds=90), 60)

    def test_gap_report(self):
        series = CandleSeries("BTC/USDT", "1m", maxlen=50)
        bars = make_candles(count=10)
        for c in bars[:5] + bars[8:]:
            series.append(c)
        gaps = gap_report(series, datetime.now(UTC))
        assert gaps and gaps[0][1] == 3

    def test_book_helpers(self, book: OrderBook):
        assert book.microprice() is not None
        assert book.notional_depth(__import__("tradebot.core.models", fromlist=["Side"]).Side.BUY) > 0
