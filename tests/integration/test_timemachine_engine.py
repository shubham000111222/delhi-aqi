"""Research candidate hook wired through the real engine; no live/OOS claims."""
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tradebot.config import Mode
from tradebot.core.clock import Clock, SimClock
from tradebot.core.models import OrderType, Side, Signal
from tradebot.core.store import Store
from tradebot.engine import Engine

START = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
async def engine(fast_cfg):
    built = await Engine.build(fast_cfg, clock=SimClock(START), store=Store(':memory:'))
    yield built
    built.close()


@pytest.fixture
def signal():
    return Signal('research-trade', 'test', 'BTC/USDT', '5m', Side.BUY,
                  OrderType.MARKET, START, Decimal('99'), Decimal('102'))


@pytest.mark.parametrize('multiplier', [0, .5, 1])
async def test_valid_multipliers_are_audited(engine, signal, multiplier):
    engine.set_research_candidate_gate(lambda s, now, mark: {'trade_id': s.id, 'multiplier': multiplier})
    assert await engine._research_candidate_multiplier(signal, Decimal('100')) == Decimal(str(multiplier))
    rows = [row for row in engine.store.events() if row['kind'] == 'timemachine_check']
    assert len(rows) == 1


@pytest.mark.parametrize('value', [True, -1, 1.01, .25, float('nan'), float('inf'), '1', None])
async def test_invalid_multiplier_skips(engine, signal, value):
    engine.set_research_candidate_gate(lambda s, now, mark: {'trade_id': s.id, 'multiplier': value})
    assert await engine._research_candidate_multiplier(signal, Decimal('100')) == 0


async def test_wrong_trade_and_exception_skip(engine, signal):
    engine.set_research_candidate_gate(lambda *args: {'trade_id': 'other', 'multiplier': 1})
    assert await engine._research_candidate_multiplier(signal, Decimal('100')) == 0

    def fail(*args):
        raise RuntimeError('broken analysis')

    engine.set_research_candidate_gate(fail)
    assert await engine._research_candidate_multiplier(signal, Decimal('100')) == 0


async def test_audit_failure_holds_entries(engine, signal, monkeypatch):
    engine.set_research_candidate_gate(lambda s, *args: {'trade_id': s.id, 'multiplier': 1})
    monkeypatch.setattr(engine, '_log', Mock(side_effect=OSError('disk unavailable')))
    assert await engine._research_candidate_multiplier(signal, Decimal('100')) == 0
    assert not engine.killswitch.can_open_entries()


async def test_wall_clock_attachment_refused(engine):
    engine.clock = Clock()
    with pytest.raises(PermissionError):
        engine.set_research_candidate_gate(lambda *args: {})


async def test_mode_change_after_attachment_skips(engine, signal):
    engine.set_research_candidate_gate(lambda s, *args: {'trade_id': s.id, 'multiplier': 1})
    engine.cfg.settings.mode = Mode.LIVE
    assert await engine._research_candidate_multiplier(signal, Decimal('100')) == 0


async def test_skip_precedes_risk_approval(engine, signal, monkeypatch):
    engine.set_research_candidate_gate(lambda s, *args: {'trade_id': s.id, 'multiplier': 0})
    monkeypatch.setattr(engine, 'market_session_note', lambda: None)
    risk = Mock(side_effect=AssertionError('risk must not run after skip'))
    monkeypatch.setattr(engine.risk, 'approve_entry', risk)
    await engine._try_enter(signal, SimpleNamespace(mark=Decimal('100')))
    risk.assert_not_called()
    assert engine.stats.vetoes == 1


@pytest.mark.parametrize('multiplier', [.5, 1])
async def test_reduction_is_passed_to_unchanged_risk(engine, signal, monkeypatch, multiplier):
    engine.set_research_candidate_gate(lambda s, *args: {'trade_id': s.id, 'multiplier': multiplier})
    monkeypatch.setattr(engine, 'market_session_note', lambda: None)
    from tradebot.risk.engine import RiskContext

    context = RiskContext(START, *(Decimal('1000'),)*5, size_multiplier=Decimal('.4'))
    monkeypatch.setattr(engine, '_risk_context', lambda *args: context)
    seen = []

    def reject(s, ctx, rules):
        seen.append(ctx.size_multiplier)
        return SimpleNamespace(approved=False, verdict=engine._veto('test_risk', 'risk still controls entry'))

    monkeypatch.setattr(engine.risk, 'approve_entry', reject)
    await engine._try_enter(signal, SimpleNamespace(mark=Decimal('100')))
    assert seen == [Decimal('.4') * Decimal(str(multiplier))]
    assert context.size_multiplier == Decimal('.4')
    assert engine.stats.orders == 0


async def test_real_checker_short_history_is_persisted_skip(engine, signal):
    from tradebot.analysis.engine_gate import ReplayBinding, TimeMachineEngineGate
    from tradebot.analysis.timemachine import TimeMachine

    alerts = []
    machine = TimeMachine(alerts.append)
    engine.set_research_candidate_gate(TimeMachineEngineGate(
        machine, lambda symbol, cutoff: (), {'test': ReplayBinding(lambda past: None, 'test-v1')},
        fee_rate=.001, slippage_rate=.0005))
    assert await engine._research_candidate_multiplier(signal, Decimal('100')) == 0
    rows = engine.store.events(kinds=['timemachine_check'])
    assert len(rows) == 1 and alerts
    assert rows[0]['payload']['rule_version'] == 'test-v1'
    assert rows[0]['payload']['trade_id'] == signal.id
    assert rows[0]['payload']['multiplier'] == 0


async def test_bridge_forwards_actual_plan_identity_cutoff_and_costs(engine, signal):
    from tradebot.analysis.engine_gate import ReplayBinding, TimeMachineEngineGate

    calls = []
    checker = Mock()
    checker.check.return_value = {'trade_id': signal.id, 'multiplier': .5, 'verdict': 'DOWN'}

    def history(symbol, cutoff):
        calls.append((symbol, cutoff))
        return ()

    rule = Mock()
    engine.set_research_candidate_gate(TimeMachineEngineGate(
        checker, history, {'test': ReplayBinding(rule, 'frozen-v2', 48)},
        fee_rate=.002, slippage_rate=.001, volatility_scale=1.2, coin_flip_skip=False))
    assert await engine._research_candidate_multiplier(signal, Decimal('100')) == Decimal('.5')
    args, kwargs = checker.check.call_args
    assert calls == [(signal.symbol, START)]
    assert args[1:3] == (signal.symbol, START)
    assert (args[3].entry, args[3].stop, args[3].target, args[3].side, args[3].max_bars) == (100, 99, 102, 1, 48)
    assert args[4:] == (signal.id, rule)
    assert kwargs == {'fee_rate': .002, 'slippage_rate': .001, 'rule_version': 'frozen-v2',
                      'volatility_scale': 1.2, 'coin_flip_skip': False}


@pytest.mark.parametrize('unsupported', ['limit', 'missing_rule', 'future'])
async def test_bridge_unsupported_candidate_skips_before_checker(engine, signal, unsupported):
    from dataclasses import replace
    from datetime import timedelta

    from tradebot.analysis.engine_gate import ReplayBinding, TimeMachineEngineGate

    checker = Mock()
    if unsupported == 'limit':
        signal = replace(signal, entry_type=OrderType.LIMIT)
    if unsupported == 'future':
        signal = replace(signal, ts=START + timedelta(minutes=1))
    bindings = {} if unsupported == 'missing_rule' else {'test': ReplayBinding(lambda past: None, 'v1')}
    engine.set_research_candidate_gate(TimeMachineEngineGate(
        checker, lambda *args: (), bindings, fee_rate=0, slippage_rate=0))
    assert await engine._research_candidate_multiplier(signal, Decimal('100')) == 0
    checker.check.assert_not_called()


async def test_real_bootstrap_reaches_engine_audit_with_point_in_time_history(engine, signal, monkeypatch):
    from datetime import timedelta

    import tradebot.analysis.timemachine as tm
    from tradebot.analysis.engine_gate import ReplayBinding, TimeMachineEngineGate
    from tradebot.core.models import Candle

    # Freeze only the budget clock: this verifies wiring, not latency acceptance.
    monkeypatch.setattr(tm, 'time', SimpleNamespace(perf_counter=lambda: 0.))
    bars = []
    for i in range(60 * 288):
        price = Decimal('100') + Decimal(i % 7) / 100
        bars.append(Candle(signal.symbol, '5m', START - timedelta(days=60) + timedelta(minutes=5*i),
                           price, price + 1, price - 1, price + Decimal('.01'), Decimal('1')))
    # Invalid future values must not contaminate the earlier decision.
    bars.append(Candle(signal.symbol, '5m', START, *(Decimal('NaN'),)*4, Decimal('1')))
    machine = tm.TimeMachine(lambda message: None)
    engine.set_research_candidate_gate(TimeMachineEngineGate(
        machine, lambda *args: bars, {'test': ReplayBinding(lambda past: None, 'v1', 6)},
        fee_rate=.001, slippage_rate=.0005))
    multiplier = await engine._research_candidate_multiplier(signal, Decimal('100'))
    assert multiplier in (Decimal('0'), Decimal('.5'), Decimal('1'))
    payload = engine.store.events(kinds=['timemachine_check'])[0]['payload']
    assert payload['seed'] == tm.trade_seed(signal.id)
    assert payload['path_count'] == 10000
    assert len(payload['input_hash']) == 64
    assert 'simulation' in payload and 'replay' in payload
    assert payload['decision_time'] == START.isoformat()
