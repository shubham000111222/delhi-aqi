from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.backtest.timemachine_windows import ResearchWindow, run_window_studies, validate_windows
from tradebot.core.models import Candle

START = datetime(2024, 1, 1, tzinfo=UTC)
WINDOWS = tuple(ResearchWindow(START+timedelta(hours=i), START+timedelta(hours=i, minutes=20)) for i in range(3))
HOLDOUT = START+timedelta(hours=3)


@pytest.mark.parametrize('damage', ['few', 'overlap', 'unordered', 'holdout', 'naive'])
def test_invalid_windows_refused(damage):
    windows = list(WINDOWS)
    if damage == 'few':
        windows.pop()
    elif damage == 'overlap':
        windows[1] = ResearchWindow(WINDOWS[0].start, WINDOWS[1].end)
    elif damage == 'unordered':
        windows.reverse()
    elif damage == 'holdout':
        windows[-1] = ResearchWindow(WINDOWS[-1].start, HOLDOUT+timedelta(minutes=1))
    else:
        windows[0] = ResearchWindow(START.replace(tzinfo=None), WINDOWS[0].end)
    with pytest.raises(ValueError):
        validate_windows(windows, HOLDOUT)


async def test_three_real_windows_do_not_promote_zero_trade_studies(fast_cfg):
    execution = {symbol: tuple(Candle(symbol, '1m', window.start+timedelta(minutes=i),
                                      Decimal(100), Decimal(101), Decimal(99), Decimal(100), Decimal(1))
                                for window in WINDOWS for i in range(20))
                 for symbol in fast_cfg.symbol_names()}
    result = await run_window_studies(fast_cfg, execution, {}, windows=WINDOWS,
                                      final_holdout_start=HOLDOUT, fee_rate=.001, slippage_rate=0,
                                      cost_stress=True)
    assert result.get('complete'), result
    assert len(result['windows']) == 3
    assert not any(result['research_screen_passed'].values())
    assert not result['filter_enabled'] and not result['provenance_verified']
    assert not result['parameters_refit'] and not result['holdout_used']
    assert len({row['study']['settings_hash'] for row in result['windows']}) == 1


async def test_every_window_must_pass_and_incomplete_or_changed_settings_fail_closed(fast_cfg, monkeypatch):
    import tradebot.backtest.timemachine_windows as module

    for damage in ('one_rejected', 'incomplete', 'changed_settings'):
        calls = []

        async def fixture_study(*args, calls=calls, damage=damage, **kwargs):
            calls.append(kwargs)
            last = len(calls) == 3
            passed = not (last and damage == 'one_rejected')
            return {'complete': not(last and damage == 'incomplete'),
                    'settings_hash': 'changed' if last and damage == 'changed_settings' else 'fixed',
                    'retention_criteria_passed': {'simulation_only': passed, 'replay_only': passed, 'both': passed},
                    'stress_survivors': {'simulation_only': True, 'replay_only': True, 'both': True}}

        monkeypatch.setattr(module, 'run_engine_study', fixture_study)
        result = await run_window_studies(fast_cfg, {}, {}, windows=WINDOWS,
                                          final_holdout_start=HOLDOUT, fee_rate=.001, slippage_rate=0)
        assert not any(result['research_screen_passed'].values())
        assert result['complete'] == (damage == 'one_rejected')
        assert all(call['cost_stress'] for call in calls)


def test_window_manifest_cli_executes_and_keeps_filter_disabled(fast_cfg, tmp_path):
    import json

    from tradebot.backtest.timemachine_cli import main
    from tradebot.data.archive import Archive

    archive = Archive(tmp_path / 'archive')
    archive.write_candles([Candle(symbol, '1m', w.start+timedelta(minutes=i),
                                  Decimal(100), Decimal(101), Decimal(99), Decimal(100), Decimal(1))
                           for symbol in fast_cfg.symbol_names() for w in WINDOWS for i in range(20)])
    manifest = tmp_path / 'windows.json'
    manifest.write_text(json.dumps([{'start': w.start.isoformat(), 'end': w.end.isoformat()} for w in WINDOWS]))
    output = tmp_path / 'evidence.json'
    assert main(['--config-dir', str(fast_cfg.settings.config_dir), '--archive-root', str(archive.root),
                 '--windows-file', str(manifest), '--holdout-start', HOLDOUT.isoformat(),
                 '--fee-rate', '.001', '--slippage-rate', '0', '--cost-stress', '--output', str(output)]) == 0
    report = json.loads(output.read_text())
    assert report['complete'] and len(report['windows']) == 3
    assert not report['filter_enabled'] and not any(report['research_screen_passed'].values())
