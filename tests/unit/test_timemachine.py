from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from tradebot.analysis.timemachine import (
    HistoryError,
    PastBars,
    ReplayPlan,
    prepare_history,
    replay_rule,
    trade_seed,
)
from tradebot.core.models import Candle

END = datetime(2024, 7, 1, tzinfo=UTC)


@pytest.fixture(scope='module')
def bars():
    start = END - timedelta(days=60)
    result = []
    for i in range(60 * 288):
        price = Decimal('100') + Decimal(i % 7) / 100
        result.append(Candle('BTC/USDT', '5m', start + timedelta(minutes=5*i),
                             price, price + 1, price - 1, price + Decimal('0.01'), Decimal('1')))
    return tuple(result)


@pytest.fixture(scope='module')
def history(bars):
    return prepare_history(bars, 'BTC/USDT', END)


def test_future_data_does_not_change_past_inputs(bars, history):
    future = replace(bars[-1], ts=END, close=Decimal('NaN'))
    result = prepare_history((*bars, future), 'BTC/USDT', END)
    assert result.input_hash == history.input_hash
    np.testing.assert_array_equal(result.standardized_ohlc, history.standardized_ohlc)
    assert trade_seed('trade1') == trade_seed('trade1') != trade_seed('trade2')
    with pytest.raises(ValueError):
        result.standardized_ohlc[0, 0] = 0


@pytest.mark.parametrize('damage', ['nan', 'gap', 'duplicate', 'short', 'timeframe'])
def test_invalid_history_fails(bars, damage):
    data = list(bars)
    if damage == 'nan':
        data[-1] = replace(data[-1], high=Decimal('NaN'))
    elif damage == 'gap':
        del data[100]
    elif damage == 'duplicate':
        data.append(data[-1])
    elif damage == 'short':
        data = data[-100:]
    else:
        data[-1] = replace(data[-1], timeframe='1m')
    with pytest.raises(HistoryError):
        prepare_history(data, 'BTC/USDT', END)


def test_prefix_cannot_read_future(bars):
    view = PastBars(bars, 2)
    assert len(view[:100]) == 2 and view[-1] == bars[1]
    with pytest.raises(IndexError):
        _ = view[2]


def test_replay_costs_reduce_net_and_twenty_trigger_requirement(history):
    def rule(past):
        assert past[-1].close_time <= END
        return ReplayPlan(100, 90, 110, max_bars=1) if len(past) % 500 == 0 else None

    free = replay_rule(history, rule, fee_rate=0, slippage_rate=0)
    costly = replay_rule(history, rule, fee_rate=.001, slippage_rate=.001)
    assert free['triggers'] >= 20 and free['insufficient'] is False
    assert costly['expected_net_r'] < free['expected_net_r']
    assert costly['counts']['time_exit'] == costly['triggers']


@pytest.mark.parametrize('side', [1, -1])
def test_same_bar_ambiguity_is_counted_stop_first(history, side):
    def rule(past):
        return ReplayPlan(100, 100 - side*.5, 100 + side*.5, side=side, max_bars=1) if len(past) == 100 else None

    result = replay_rule(history, rule, fee_rate=0, slippage_rate=0)
    assert result['ambiguous_stop_first'] == 1
    assert result['counts']['stop_first'] == 1
    assert result['expected_net_r'] < 0 and result['insufficient'] is True
    assert result['first_touch_verified'] is False


def test_stop_gap_fills_at_open_not_stop(history):
    data = list(history.bars)
    data[100] = replace(data[100], open=Decimal('100'), high=Decimal('100.5'), low=Decimal('99.5'), close=Decimal('100'))
    data[101] = replace(data[101], open=Decimal('95'), high=Decimal('96'), low=Decimal('94'), close=Decimal('95'))
    altered = prepare_history(data, 'BTC/USDT', END)
    result = replay_rule(altered, lambda past: ReplayPlan(100, 99, 110, max_bars=2)
                         if len(past) == 100 else None, fee_rate=0, slippage_rate=0)
    assert result['expected_net_r'] == -5


def test_incomplete_horizon_is_not_fabricated_time_exit(history):
    result = replay_rule(history, lambda past: ReplayPlan(100, 90, 110, max_bars=288)
                         if len(past) == len(history.bars)-2 else None, fee_rate=0, slippage_rate=0)
    assert result['triggers'] == 0 and result['censored_triggers'] == 1
    assert result['expected_net_r'] is None


def test_risk_package_does_not_import_analysis_or_ai():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / 'src/tradebot/risk'
    for path in root.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not {'analysis', 'ai'}.intersection((node.module or '').split('.'))
            if isinstance(node, ast.Import):
                for name in node.names:
                    assert not {'analysis', 'ai'}.intersection(name.name.split('.'))


def test_bootstrap_same_seed_costs_and_probability_mass(history):
    from tradebot.analysis.timemachine import simulate_trade

    plan = ReplayPlan(100, 99, 101, max_bars=48)
    free = simulate_trade(history, plan, 'trade1', fee_rate=0, slippage_rate=0)
    again = simulate_trade(history, plan, 'trade1', fee_rate=0, slippage_rate=0)
    costs = simulate_trade(history, plan, 'trade1', fee_rate=.001, slippage_rate=.001)
    assert free == again
    assert costs['expected_net_r'] < free['expected_net_r']
    assert abs(free['target_first'] + free['stop_first'] + free['time_exit'] - 1) < 1e-12
    assert free['path_count'] == 10000
    assert free['first_touch_verified'] is False


def test_symmetric_bootstrap_is_not_a_one_sided_prediction(history):
    from tradebot.analysis.timemachine import simulate_trade

    rng = np.random.default_rng(42)
    returns = rng.normal(0, .001, len(history.bars)-1)
    returns -= returns.mean()
    ohlc = np.column_stack((np.zeros(len(returns)), np.maximum(returns, 0),
                           np.minimum(returns, 0), returns))
    symmetric = replace(history, standardized_ohlc=ohlc / .001, volatility=.001)
    result = simulate_trade(symmetric, ReplayPlan(100, 99, 101, max_bars=96),
                            'symmetric', fee_rate=0, slippage_rate=0)
    assert abs(result['target_first'] - result['stop_first']) < .05


@pytest.mark.parametrize('mean,tail,timed,replay_mean,insufficient,skip,expected', [
    (.1, -1., .3, None, True, True, 1.),
    (.1, -1., .3, 0., False, True, 1.),
    (.2, -1., .3, -.1, False, True, .5),
    (.06, -1., .3, None, True, True, .5),
    (.04, -1., .3, None, True, True, 0.),
    (.04, -1., .3, None, True, False, .5),
    (0., -1., .3, .2, False, False, 0.),
    (-.1, -1., .3, .2, False, False, 0.),
    (.2, -1.51, .3, .2, False, False, 0.),
    (.04, -1., .61, .2, False, False, 0.),
])
def test_fixed_verdicts_never_increase_risk(mean, tail, timed, replay_mean, insufficient, skip, expected):
    from tradebot.analysis.timemachine import trade_verdict

    result = trade_verdict({'expected_net_r': mean, 'p05_r': tail, 'time_exit': timed,
                            'target_first': (1-timed)/2, 'stop_first': (1-timed)/2},
                           {'insufficient': insufficient, 'expected_net_r': replay_mean},
                           coin_flip_skip=skip)
    assert result['multiplier'] == expected
    assert 0 <= result['multiplier'] <= 1
    assert result['filter_enabled'] is False
    if abs(mean) < .05:
        assert result['label'] == 'coin flip'


def test_invalid_verdict_fails_closed():
    from tradebot.analysis.timemachine import trade_verdict

    assert trade_verdict({}, {})['multiplier'] == 0
    assert trade_verdict({'expected_net_r': float('nan')}, {})['multiplier'] == 0
