from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradebot.backtest.timemachine_engine_study import run_engine_study
from tradebot.config import Mode
from tradebot.core.models import Candle
from tradebot.engine import Engine

START = datetime(2024, 1, 1, tzinfo=UTC)
END = START + timedelta(minutes=20)


def tape():
    return {symbol: tuple(Candle(symbol, '1m', START + timedelta(minutes=i),
                                 Decimal(100), Decimal(101), Decimal(99), Decimal(100), Decimal(1))
                           for i in range(20)) for symbol in ('BTC/USDT', 'ETH/USDT')}



async def test_real_four_engines_have_separate_state_and_no_zero_trade_promotion(fast_cfg, monkeypatch):
    built = []
    original = Engine.build

    async def build(*args, **kwargs):
        engine = await original(*args, **kwargs)
        built.append(engine)
        return engine

    monkeypatch.setattr(Engine, 'build', build)
    old_state = fast_cfg.settings.state_dir
    result = await run_engine_study(fast_cfg, tape(), {}, start=START, end=END,
                                    final_holdout_start=END, fee_rate=.001, slippage_rate=.0005)
    assert result.get('complete'), result
    assert len(built) == 4
    assert len({id(e.store) for e in built}) == 4
    assert len({id(e.ledger) for e in built}) == 4
    assert len({id(e.clock) for e in built}) == 4
    assert len({id(e.strategies) for e in built}) == 4
    assert built[0]._research_candidate_gate is None
    assert [e._research_candidate_gate.variant for e in built[1:]] == ['simulation_only', 'replay_only', 'both']
    assert not any(result['retention_criteria_passed'].values())
    assert not result['filter_enabled'] and not result['provenance_verified']
    assert fast_cfg.settings.state_dir == old_state
    assert result['engine_isolation'] and result['cold_start']
    assert all(row['stats']['bars'] == 40 for row in result['diagnostics'].values())


async def test_future_append_does_not_change_study_hashes(fast_cfg):
    first = await run_engine_study(fast_cfg, tape(), {}, start=START, end=END,
                                   final_holdout_start=END, fee_rate=.001, slippage_rate=.0005)
    future = replace(tape()['BTC/USDT'][-1], ts=END, close=Decimal('NaN'))
    extra = {**tape(), 'BTC/USDT': (*tape()['BTC/USDT'], future)}
    second = await run_engine_study(fast_cfg, extra, {}, start=START, end=END,
                                    final_holdout_start=END, fee_rate=.001, slippage_rate=.0005)
    assert first.get('complete') and second.get('complete'), (first, second)
    assert first['input_hash'] == second['input_hash']
    assert first['settings_hash'] == second['settings_hash']


async def test_holdout_and_live_rejected_before_build(fast_cfg, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('engine must not be built')

    monkeypatch.setattr(Engine, 'build', forbidden)
    result = await run_engine_study(fast_cfg, tape(), {}, start=START, end=END,
                                    final_holdout_start=START, fee_rate=.001, slippage_rate=0)
    assert not result['complete'] and 'holdout' in result['error']
    fast_cfg.settings.mode = Mode.LIVE
    result = await run_engine_study(fast_cfg, tape(), {}, start=START, end=END,
                                    final_holdout_start=END, fee_rate=.001, slippage_rate=0)
    assert not result['complete'] and 'paper mode' in result['error']


def test_research_cli_writes_disabled_evidence_and_refuses_overwrite(fast_cfg, tmp_path):
    import json

    from tradebot.backtest.timemachine_cli import main
    from tradebot.data.archive import Archive

    archive = Archive(tmp_path / 'archive')
    archive.write_candles([bar for values in tape().values() for bar in values])
    archive.write_candles([replace(tape()['BTC/USDT'][0], timeframe='5m', ts=START-timedelta(minutes=5))])
    output = tmp_path / 'study.json'
    args = ['--config-dir', str(fast_cfg.settings.config_dir), '--archive-root', str(archive.root),
            '--start', START.isoformat(), '--end', END.isoformat(), '--holdout-start', END.isoformat(),
            '--fee-rate', '.001', '--slippage-rate', '.0005', '--cost-stress', '--output', str(output)]
    assert main(args) == 0
    evidence = json.loads(output.read_text())
    assert evidence['complete'] and not evidence['filter_enabled']
    assert evidence['cost_stress']['multiplier'] == 2
    before = output.read_bytes()
    assert main(args) == 2
    assert output.read_bytes() == before


def test_cli_naive_timestamp_refused_without_evidence_file(tmp_path):
    from tradebot.backtest.timemachine_cli import main

    output = tmp_path / 'refused.json'
    assert main(['--archive-root', str(tmp_path), '--start', '2024-01-01', '--end', END.isoformat(),
                 '--holdout-start', END.isoformat(), '--fee-rate', '0', '--slippage-rate', '0',
                 '--output', str(output)]) == 2
    assert not output.exists()


async def test_incomplete_engine_run_cannot_produce_retention(fast_cfg, monkeypatch):
    async def early_exit(self, **kwargs):
        return self.stats

    monkeypatch.setattr(Engine, 'run', early_exit)
    result = await run_engine_study(fast_cfg, tape(), {}, start=START, end=END,
                                    final_holdout_start=END, fee_rate=.001, slippage_rate=0)
    assert not result['complete'] and 'incomplete engine execution' in result['error']
    assert not any(result['retention_criteria_passed'].values())


async def test_missing_symbol_gap_and_invalid_prices_refused_before_engine_build(fast_cfg, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('engine should not be built for invalid inputs')

    monkeypatch.setattr(Engine, 'build', forbidden)
    bad_inputs = []
    missing = tape()
    del missing['ETH/USDT']
    bad_inputs.append(missing)
    gap = tape()
    gap['BTC/USDT'] = (*gap['BTC/USDT'][:3], *gap['BTC/USDT'][4:])
    bad_inputs.append(gap)
    invalid = tape()
    invalid['BTC/USDT'] = (replace(invalid['BTC/USDT'][0], high=Decimal('NaN')),
                            *invalid['BTC/USDT'][1:])
    bad_inputs.append(invalid)
    for data in bad_inputs:
        result = await run_engine_study(fast_cfg, data, {}, start=START, end=END,
                                        final_holdout_start=END, fee_rate=.001, slippage_rate=0)
        assert not result['complete']
        assert 'AssertionError' not in result['error']
        assert not any(result['retention_criteria_passed'].values())


async def test_cost_stress_runs_eight_fresh_engines_with_changed_costs(fast_cfg, monkeypatch):
    built = []
    original = Engine.build

    async def build(*args, **kwargs):
        engine = await original(*args, **kwargs)
        built.append(engine)
        return engine

    monkeypatch.setattr(Engine, 'build', build)
    original_fee = fast_cfg.settings.paper.taker_fee
    result = await run_engine_study(fast_cfg, tape(), {}, start=START, end=END,
                                    final_holdout_start=END, fee_rate=.001, slippage_rate=.0005,
                                    cost_stress=True)
    assert result.get('complete'), result
    assert len(built) == 8 and len({id(e.store) for e in built}) == 8
    assert all(e.cfg.settings.paper.taker_fee == original_fee for e in built[:4])
    assert all(e.cfg.settings.paper.taker_fee == original_fee*2 for e in built[4:])
    assert all(e.cfg.settings.paper.slippage_multiplier == fast_cfg.settings.paper.slippage_multiplier*2
               for e in built[4:])
    assert built[5]._research_candidate_gate.fee_rate == .002
    assert built[5]._research_candidate_gate.slippage_rate == .001
    assert fast_cfg.settings.paper.taker_fee == original_fee
    stressed = result['cost_stress']['study']
    assert result['input_hash'] == stressed['input_hash']
    assert result['settings_hash'] != stressed['settings_hash']
    assert not any(result['stress_survivors'].values())
    assert not result['filter_enabled'] and not result['cost_stress']['venue_costs_verified']


async def test_cost_stress_failure_invalidates_retention(fast_cfg):
    # Doubling this already-high multiplier exceeds the simulator's bounded model.
    fast_cfg.settings.paper.slippage_multiplier = Decimal('6')
    result = await run_engine_study(fast_cfg, tape(), {}, start=START, end=END,
                                    final_holdout_start=END, fee_rate=.001, slippage_rate=0,
                                    cost_stress=True)
    assert not result['complete']
    assert not any(result['retention_criteria_passed'].values())
    assert not result['filter_enabled']


async def test_terminal_orders_cannot_qualify_even_with_permissive_metric_comparison(fast_cfg, monkeypatch):
    import tradebot.backtest.timemachine_engine_study as module

    original = Engine.run
    counter = [0]

    async def run(self, **kwargs):
        await original(self, **kwargs)
        # Inject an unresolved pending entry into the BASELINE after all bars close.
        if counter[0] == 0:
            self.om.pending['unfinished'] = object()
        counter[0] += 1
        return self.stats

    def permissive(window, callback):
        return {'complete': True, 'retention_criteria_passed': {
            'simulation_only': True, 'replay_only': True, 'both': True}}

    monkeypatch.setattr(Engine, 'run', run)
    monkeypatch.setattr(module, 'run_study', permissive)
    result = await run_engine_study(fast_cfg, tape(), {}, start=START, end=END,
                                    final_holdout_start=END, fee_rate=.001, slippage_rate=0)
    assert result.get('complete'), result
    assert result['diagnostics']['no_filter']['terminal_censored']
    assert result['diagnostics']['no_filter']['terminal_state']['pending_entries'] == 1
    assert not any(result['retention_criteria_passed'].values())


async def test_open_terminal_position_is_disclosed_without_invented_liquidation(fast_cfg, monkeypatch):
    from tradebot.core.models import Position

    original = Engine.run

    async def run(self, **kwargs):
        await original(self, **kwargs)
        self.ledger.positions[('BTC/USDT', 'injected')] = Position(
            'BTC/USDT', 'injected', Decimal('1'), Decimal('90'), Decimal('80'),
            Decimal('120'), Decimal('80'), START)
        return self.stats

    monkeypatch.setattr(Engine, 'run', run)
    result = await run_engine_study(fast_cfg, tape(), {}, start=START, end=END,
                                    final_holdout_start=END, fee_rate=.001, slippage_rate=0)
    assert result.get('complete'), result
    for row in result['diagnostics'].values():
        assert row['terminal_censored'] and not row['comparison_eligible']
        assert len(row['terminal_state']['positions']) == 1
        assert row['stats']['trades_closed'] == 0
    assert not any(result['retention_criteria_passed'].values())


async def test_project_gates_remain_distinct_from_research_retention(fast_cfg):
    result = await run_engine_study(fast_cfg, tape(), {}, start=START, end=END,
                                    final_holdout_start=END, fee_rate=.001, slippage_rate=0,
                                    cost_stress=True)
    assert result.get('complete'), result
    assert not result['acceptance_gates_passed']
    for row in result['diagnostics'].values():
        gates = {g['name']: g for g in row['acceptance_gates']}
        assert row['metrics']['trade_count'] == 0
        assert row['monte_carlo'] is None  # no manufactured MC evidence from no trades
        assert not row['acceptance_gates_passed']
        for name in ('out_of_sample_provenance', 'trade_count', 'profit_factor',
                     'sharpe', 'monte_carlo_drawdown', 'cost_stress_2x'):
            assert not gates[name]['passed']
        assert '200' in gates['trade_count']['detail']
        assert '1.3' in gates['profit_factor']['detail']
        assert gates['study_comparison_integrity']['passed']
    assert not result['filter_enabled']


async def test_nonzero_trade_engine_path_and_short_history_filter_suppression(fast_cfg):
    # Artificial market-only fixture exercises plumbing, NOT production defaults,
    # tuning, OOS performance or a recommendation to alter limit strategies.
    for name, strategy in list(fast_cfg.strategies.strategies.items()):
        market_ema = name == 'S1_ema_trend'
        fast_cfg.strategies.strategies[name] = strategy.model_copy(update={
            'enabled': market_ema,
            'params': {**strategy.params, 'entry_type': 'market', 'limit_offset_bps': 0,
                       'adx_min': 0, 'target_r': .5, 'stop_atr_mult': .5}})
    fast_cfg = replace(fast_cfg, risk=fast_cfg.risk.model_copy(update={'min_target_cost_ratio': Decimal('0')}))
    length = 1800
    execution = {}
    for symbol in fast_cfg.symbol_names():
        rows = []
        for i in range(length):
            price = Decimal('100') + Decimal(i) / 50
            rows.append(Candle(symbol, '1m', START+timedelta(minutes=i), price,
                                price+Decimal('.1'), price-Decimal('.1'), price+Decimal('.01'), Decimal('10')))
        execution[symbol] = rows
    end = START+timedelta(minutes=length)
    result = await run_engine_study(fast_cfg, execution, {}, start=START, end=end,
                                    final_holdout_start=end, fee_rate=.001, slippage_rate=.0005)
    assert result.get('complete'), result
    baseline = result['diagnostics']['no_filter']
    assert baseline['stats']['signals'] > 0
    assert baseline['stats']['orders'] > 0, baseline
    assert baseline['stats']['fills'] > 0
    assert baseline['metrics']['trade_count'] > 0, baseline
    assert baseline['monte_carlo'] is not None
    assert not baseline['trade_records_truncated']
    assert len(baseline['closed_trades']) == baseline['metrics']['trade_count']
    assert sum(t['fees'] for t in baseline['closed_trades']) > 0
    assert all(START <= t['opened_at'] <= t['closed_at'] <= end for t in baseline['closed_trades'])
    for variant in ('simulation_only', 'replay_only', 'both'):
        row = result['diagnostics'][variant]
        assert row['checks'] > 0 and row['invalid_checks'] > 0
        assert row['stats']['orders'] == 0
        assert not result['retention_criteria_passed'][variant]
    assert not result['filter_enabled'] and not result['acceptance_gates_passed']
