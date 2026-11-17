from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.analysis.strategy_replay import StrategyReplayRule, closed_strategy_series
from tradebot.config import RegimeConfig, StrategyConfig
from tradebot.core.models import Candle
from tradebot.strategies.base import StrategyContext
from tradebot.strategies.regime import atr_value, detect_regime
from tradebot.strategies.registry import build_strategy

START = datetime(2024, 1, 1, tzinfo=UTC)


def bars(count=300):
    return tuple(Candle('BTC/USDT', '5m', START + timedelta(minutes=5*i),
                        Decimal(100+i), Decimal(102+i), Decimal(99+i), Decimal(101+i), Decimal(1))
                 for i in range(count))


def config(entry='market'):
    return StrategyConfig(class_name='tradebot.strategies.ema_trend:EmaTrend',
                          timeframes=('15m',), regimes=(), params={'entry_type': entry, 'adx_min': 0})


def test_exact_strategy_entry_stop_target_parity():
    data = bars()
    cfg = config()
    regime = RegimeConfig()
    rule = StrategyReplayRule('ema', cfg, '15m', regime, max_bars=48)
    series = closed_strategy_series(data, '15m')
    last = series[-1]
    ctx = StrategyContext(last.symbol, '15m', series, last.ts, last.close,
                          atr_value(series, regime.adx_period), detect_regime(series, regime), Decimal(1000))
    signal = build_strategy('ema', cfg).evaluate(ctx)
    assert signal is not None
    plan = rule(data)
    assert plan is not None
    assert (plan.entry, plan.stop, plan.target, plan.side, plan.max_bars) == (
        float(last.close), float(signal.stop), float(signal.target), 1, 48)


def test_aggregation_incomplete_tail_not_used_or_repeated():
    data = bars(301)
    series = closed_strategy_series(data, '15m')
    assert len(series) == 100
    assert series[0].open == data[0].open and series[0].close == data[2].close
    assert series[0].volume == 3
    assert StrategyReplayRule('ema', config(), '15m', RegimeConfig())(data) is None


def test_config_snapshot_version_and_prefix_determinism():
    cfg = config()
    rule = StrategyReplayRule('ema', cfg, '15m', RegimeConfig())
    data = bars()
    plan = rule(data)
    cfg.params['target_r'] = 100
    assert rule(data) == plan
    assert StrategyReplayRule('ema', cfg, '15m', RegimeConfig()).version != rule.version
    extended = (*data, replace(data[-1], ts=data[-1].ts + timedelta(minutes=5), close=Decimal('NaN')))
    assert rule(extended[:len(data)]) == plan
    assert rule.binding().version == rule.version


def test_limit_entry_is_not_replaced_with_market():
    rule = StrategyReplayRule('ema', config('limit'), '15m', RegimeConfig())
    with pytest.raises(ValueError, match='limit entry'):
        rule(bars())


@pytest.mark.parametrize('damage', ['gap', 'duplicate', 'symbol', 'timeframe'])
def test_invalid_prefix_rejected(damage):
    data = list(bars(6))
    if damage == 'gap':
        del data[1]
    elif damage == 'duplicate':
        data[1] = data[0]
    elif damage == 'symbol':
        data[1] = replace(data[1], symbol='ETH/USDT')
    else:
        data[1] = replace(data[1], timeframe='1m')
    with pytest.raises(ValueError):
        closed_strategy_series(data, '15m')


def test_stateful_class_and_wrong_timeframe_refused():
    cfg = config()
    with pytest.raises(ValueError):
        StrategyReplayRule('ema', cfg, '1h', RegimeConfig())
    with pytest.raises(ValueError):
        StrategyReplayRule('ml', cfg.model_copy(update={'class_name': 'tradebot.strategies.ml_signal:MLSignal'}),
                           '15m', RegimeConfig())
