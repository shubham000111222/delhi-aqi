"""Versioned, closed-prefix replay of existing deterministic strategy opinions.

Entry/stop/target replay only, not parity with portfolio/risk/managed exits. Limit
signals are deliberately refused until entry queue/fill semantics are implemented.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal

from ..config import RegimeConfig, StrategyConfig
from ..core.clock import floor_ts, timeframe_seconds
from ..core.models import Candle, OrderType, Side
from ..data.series import CandleSeries
from ..strategies.base import StrategyContext
from ..strategies.regime import atr_value, detect_regime
from ..strategies.registry import build_strategy
from .engine_gate import ReplayBinding
from .timemachine import ReplayPlan

SUPPORTED = frozenset({'tradebot.strategies.ema_trend:EmaTrend',
                       'tradebot.strategies.meanrev:MeanReversion',
                       'tradebot.strategies.breakout:Breakout'})


def closed_strategy_series(bars: Sequence[Candle], timeframe: str) -> CandleSeries:
    """Aggregate only complete aligned groups; reject missing/duplicate input."""
    seconds = timeframe_seconds(timeframe)
    if seconds < 300 or seconds % 300:
        raise ValueError('strategy timeframe must be a multiple of 5m')
    if not bars:
        raise ValueError('empty strategy prefix')
    ratio = seconds // 300
    series = CandleSeries(bars[-1].symbol, timeframe, maxlen=1500)
    # One extra group permits dropping a partial oldest bucket.
    start = max(0, len(bars) - (1501 * ratio))
    group: list[Candle] = []
    stamp = None
    previous = None
    for i in range(start, len(bars)):
        bar = bars[i]
        if bar.symbol != series.symbol or bar.timeframe != '5m':
            raise ValueError('strategy replay requires one symbol of 5m bars')
        if previous is not None and (bar.ts - previous.ts).total_seconds() != 300:
            raise ValueError('strategy replay input has gaps or duplicates')
        previous = bar
        bucket = floor_ts(bar.ts, seconds)
        if bucket != stamp:
            group = []
            stamp = bucket
        group.append(bar)
        if len(group) == ratio and group[0].ts == bucket and bar.close_time.timestamp() == bucket.timestamp()+seconds:
            series.append(Candle(series.symbol, timeframe, bucket, group[0].open,
                                 max(b.high for b in group), min(b.low for b in group),
                                 group[-1].close, sum((b.volume for b in group), Decimal(0))))
    return series


class StrategyReplayRule:
    def __init__(self, name: str, config: StrategyConfig, timeframe: str,
                 regime: RegimeConfig, *, max_bars: int = 288):
        if config.class_name not in SUPPORTED:
            raise ValueError('unsupported or stateful strategy replay class')
        if timeframe not in config.timeframes or timeframe_seconds(timeframe) % 300:
            raise ValueError('timeframe not supported by strategy/5m history')
        if type(max_bars) is not int or not 1 <= max_bars <= 2016:
            raise ValueError('invalid replay horizon')
        self._name = name
        self._config = config.model_copy(deep=True)
        self._regime = regime.model_copy(deep=True)
        self._timeframe = timeframe
        self._max_bars = max_bars
        settings = {'adapter': 'closed-prefix-strategy-v1', 'name': name,
                    'strategy': self._config.model_dump(mode='json'),
                    'regime': self._regime.model_dump(mode='json'),
                    'timeframe': timeframe, 'max_bars': max_bars, 'series_maxlen': 1500}
        self.version = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()

    def binding(self) -> ReplayBinding:
        return ReplayBinding(self, self.version, self._max_bars)

    def __call__(self, bars: Sequence[Candle]) -> ReplayPlan | None:
        if not bars:
            return None
        seconds = timeframe_seconds(self._timeframe)
        if int(bars[-1].close_time.timestamp()) % seconds:
            return None  # not a newly closed strategy bar; do not repeat a stale signal
        series = closed_strategy_series(bars, self._timeframe)
        if not series or series[-1].close_time != bars[-1].close_time:
            return None
        # Fresh pure rule instance prevents state leaking between historical prefixes.
        strategy = build_strategy(self._name, self._config)
        if len(series) < strategy.warmup:
            return None
        last = series[-1]
        context = StrategyContext(series.symbol, self._timeframe, series, last.ts, last.close,
                                  atr_value(series, self._regime.adx_period),
                                  detect_regime(series, self._regime), Decimal('1000'))
        signal = strategy.evaluate(context)
        if signal is None:
            return None
        if signal.entry_type is not OrderType.MARKET:
            raise ValueError('strategy emitted limit entry; market replay substitution forbidden')
        plan = ReplayPlan(float(last.close), float(signal.stop), float(signal.target),
                          1 if signal.side is Side.BUY else -1, self._max_bars)
        plan.validate()
        return plan
