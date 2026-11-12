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
            '--fee-rate', '.001', '--slippage-rate', '.0005', '--output', str(output)]
    assert main(args) == 0
    evidence = json.loads(output.read_text())
    assert evidence['complete'] and not evidence['filter_enabled']
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
