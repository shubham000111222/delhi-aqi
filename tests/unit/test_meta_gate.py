"""Wiring a meta-label gate into the engine: veto only, and fail open with a reason."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tradebot.ai.features import FeatureFrame
from tradebot.ai.meta import (
    LogisticGate,
    MetaEntryFilter,
    MetaGate,
    MetaReport,
    load_meta,
    save_meta,
)
from tradebot.core.models import Candle
from tradebot.meta_gate import MIN_BARS, attach, frame_builder

START = datetime(2024, 1, 1, tzinfo=UTC)


def frame(probability_features: dict[str, float] | None = None, *, rows: int = 5) -> FeatureFrame:
    features = probability_features or {"close_z_20": 0.4}
    return FeatureFrame(
        symbol="BTC/USDT",
        ts=[START + timedelta(minutes=i) for i in range(rows)],
        close=np.full(rows, 100.0),
        features={name: np.full(rows, value) for name, value in features.items()},
    )


def gate(*, coefficient: float = 2.0, threshold: float = 0.5) -> MetaGate:
    model = LogisticGate()
    model.coef_ = np.array([0.0, coefficient])
    model.mean_ = np.array([0.0])
    model.scale_ = np.array([1.0])
    return MetaGate(model=model, threshold=threshold, features=["close_z_20"])


class Signal:
    symbol = "BTC/USDT"
    strategy = "S1_ema_trend"
    id = "sig-1"


class TestFilter:
    def test_an_unready_model_lets_everything_through(self):
        unready = MetaGate(model=LogisticGate(), threshold=0.5, features=["close_z_20"])
        entry_filter = MetaEntryFilter(gate=unready, frame_for=lambda _s: frame())
        allowed, reason = entry_filter(Signal())
        assert allowed is True and "not ready" in reason

    def test_a_confident_gate_takes_the_trade(self):
        entry_filter = MetaEntryFilter(gate=gate(), frame_for=lambda _s: frame({"close_z_20": 1.5}))
        allowed, reason = entry_filter(Signal())
        assert allowed is True and "P(target before stop)" in reason

    def test_a_doubtful_gate_refuses_it_with_the_probability_in_the_reason(self):
        entry_filter = MetaEntryFilter(gate=gate(), frame_for=lambda _s: frame({"close_z_20": -1.5}))
        allowed, reason = entry_filter(Signal())
        assert allowed is False
        assert "P(target before stop)" in reason

    def test_no_frame_yet_means_no_veto(self):
        entry_filter = MetaEntryFilter(gate=gate(), frame_for=lambda _s: None)
        allowed, reason = entry_filter(Signal())
        assert allowed is True and "no features" in reason

    def test_a_missing_feature_vetoes_rather_than_raising(self):
        entry_filter = MetaEntryFilter(gate=gate(), frame_for=lambda _s: frame({"other": 1.0}))
        allowed, reason = entry_filter(Signal())
        assert allowed is False and "missing features" in reason

    def test_a_broken_frame_builder_fails_open(self):
        def explode(_symbol: str) -> FeatureFrame:
            raise RuntimeError("feature store down")

        entry_filter = MetaEntryFilter(gate=gate(), frame_for=explode)
        allowed, reason = entry_filter(Signal())
        assert allowed is True and "unavailable" in reason


class TestCard:
    def test_a_saved_card_loads_back_into_the_same_gate(self, tmp_path: Path):
        report = MetaReport(
            setups=200, base_rate=0.4, folds=4, auc=0.6, precision_at_threshold=0.55,
            recall_at_threshold=0.3, trades_taken=40, trades_skipped=160, expectancy_all=1.0,
            expectancy_taken=6.0, expectancy_skipped=-0.5, threshold=0.55,
        )
        model = gate(coefficient=1.7, threshold=0.55)
        path = tmp_path / "card.json"
        save_meta(str(path), name="meta-btc", model=model.model, features=["close_z_20"],
                  threshold=0.55, report=report)

        loaded = load_meta(str(path))
        assert loaded.ready is True
        assert loaded.name == "meta-btc"
        assert loaded.threshold == pytest.approx(0.55)
        assert loaded.features == ["close_z_20"]
        row = np.array([[0.3]])
        assert loaded.model.predict_proba(row)[0] == pytest.approx(model.model.predict_proba(row)[0])

    def test_a_card_that_is_not_a_meta_model_is_refused(self, tmp_path: Path):
        path = tmp_path / "card.json"
        path.write_text(json.dumps({"kind": "signal", "name": "S4"}))
        with pytest.raises(ValueError, match="not a meta-label"):
            load_meta(str(path))

    def test_a_card_whose_coefficients_do_not_match_its_features_is_refused(self, tmp_path: Path):
        path = tmp_path / "card.json"
        path.write_text(
            json.dumps(
                {"kind": "meta_label", "features": ["a", "b"], "coefficients": [0.1],
                 "mean": [0.0], "scale": [1.0], "threshold": 0.5}
            )
        )
        with pytest.raises(ValueError, match="coefficients"):
            load_meta(str(path))


class FakeSeries:
    def __init__(self, candles: list[Candle]) -> None:
        self._candles = candles

    def last(self, count: int) -> list[Candle]:
        return self._candles[-count:]


class FakeMarket:
    base_timeframe = "1m"
    series_maxlen = 300

    def __init__(self, candles: list[Candle], book: Any = None) -> None:
        self._candles = candles
        self._book = book

    def candles(self, symbol: str, timeframe: str) -> FakeSeries:
        del symbol, timeframe
        return FakeSeries(self._candles)

    def book(self, symbol: str) -> Any:
        del symbol
        return self._book


class FakeEngine:
    def __init__(self, candles: list[Candle]) -> None:
        self.market = FakeMarket(candles)
        self.entry_filter: Any = None


def bars(count: int) -> list[Candle]:
    out = []
    for index in range(count):
        price = Decimal("100") + Decimal(index % 7) / Decimal("10")
        out.append(
            Candle(
                symbol="BTC/USDT", timeframe="1m", ts=START + timedelta(minutes=index),
                open=price, high=price + 1, low=price - 1, close=price, volume=Decimal("5"),
            )
        )
    return out


class TestFrameBuilder:
    def test_too_few_bars_means_no_frame(self):
        builder = frame_builder(FakeEngine(bars(MIN_BARS - 1)))
        assert builder("BTC/USDT") is None

    def test_a_full_window_produces_a_frame(self):
        builder = frame_builder(FakeEngine(bars(220)))
        result = builder("BTC/USDT")
        assert result is not None
        assert result.symbol == "BTC/USDT"
        assert len(result) == 220

    def test_the_frame_is_cached_until_the_next_bar_closes(self):
        engine = FakeEngine(bars(220))
        calls = {"n": 0}
        from tradebot.ai import features as features_module

        original = features_module.FeatureStore.compute_many

        def counting(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return original(self, *args, **kwargs)

        features_module.FeatureStore.compute_many = counting  # type: ignore[method-assign]
        try:
            builder = frame_builder(engine)
            first = builder("BTC/USDT")
            second = builder("BTC/USDT")
            assert first is second
            assert calls["n"] == 1
        finally:
            features_module.FeatureStore.compute_many = original  # type: ignore[method-assign]

    def test_attach_installs_a_veto_only_filter(self, tmp_path: Path):
        report = MetaReport(
            setups=100, base_rate=0.5, folds=3, auc=0.55, precision_at_threshold=0.5,
            recall_at_threshold=0.5, trades_taken=20, trades_skipped=80, expectancy_all=2.0,
            expectancy_taken=4.0, expectancy_skipped=-1.0, threshold=0.5,
        )
        model = LogisticGate()
        model.coef_ = np.array([0.0, 1.0])
        model.mean_ = np.array([0.0])
        model.scale_ = np.array([1.0])
        path = tmp_path / "card.json"
        save_meta(str(path), name="meta-btc", model=model, features=["close_z_20"],
                  threshold=0.5, report=report)

        engine = FakeEngine(bars(220))
        entry_filter = attach(engine, str(path))
        assert engine.entry_filter is entry_filter
        allowed, reason = entry_filter(Signal())
        assert isinstance(allowed, bool) and isinstance(reason, str)


class TestEngineHook:
    """`_entry_allowed` consults the filter, and a broken filter lets the trade through."""

    def _engine(self):
        import asyncio

        from tradebot.config import load_config
        from tradebot.core.clock import SimClock
        from tradebot.core.models import OrderType, Side, Signal
        from tradebot.core.money import D
        from tradebot.core.store import Store
        from tradebot.engine import Engine

        cfg = load_config()

        async def build():
            return await Engine.build(cfg, clock=SimClock(START), store=Store(":memory:"))

        engine = asyncio.run(build())
        signal = Signal(
            id="sig1", strategy="S1_ema_trend", symbol="BTC/USDT", timeframe="1h", side=Side.BUY,
            entry_type=OrderType.LIMIT, ts=START, stop=D("1900"), target=D("2200"),
            limit_price=D("2000"),
        )
        return engine, signal

    def test_without_a_filter_every_entry_is_allowed(self):
        engine, signal = self._engine()
        try:
            assert engine.entry_filter is None
            assert engine._entry_allowed(signal, None) is True
            assert engine.stats.vetoes == 0
        finally:
            engine.close()

    def test_a_veto_is_counted_and_written_to_the_verdict_log(self):
        engine, signal = self._engine()
        try:
            engine.entry_filter = lambda _s, _c: (False, "P(target before stop) = 0.21 vs 0.60")
            assert engine._entry_allowed(signal, None) is False
            assert engine.stats.vetoes == 1
            row = engine.store._query("SELECT * FROM risk_verdicts WHERE signal_id=?", (signal.id,))
            assert row, "a veto must leave a record, or it is indistinguishable from silence"
        finally:
            engine.close()

    def test_a_broken_filter_does_not_block_trading(self):
        engine, signal = self._engine()
        try:
            def explode(_s: Any, _c: Any) -> tuple[bool, str]:
                raise RuntimeError("model file vanished")

            engine.entry_filter = explode
            assert engine._entry_allowed(signal, None) is True
            assert engine.stats.vetoes == 0
        finally:
            engine.close()
