from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tradebot.backtest.timemachine_study import StudyWindow, VariantRun, run_study

WINDOW = StudyWindow(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 3, 1, tzinfo=UTC),
                     datetime(2024, 4, 1, tzinfo=UTC), 'recorded-input', 'fixed-settings', 1000.)


def output(variant, *, net=(.1,)*20, equity=(1000., 900., 1050.)):
    return VariantRun(variant, variant+'-independent-run', WINDOW.input_hash, WINDOW.settings_hash,
                      WINDOW.start, WINDOW.end, WINDOW.starting_capital, net, equity)


def test_four_runs_and_retention_boundaries():
    calls = []

    def runner(variant, window):
        calls.append((variant, window))
        if variant == 'simulation_only':
            return output(variant, net=(.2,)*10)  # exactly 50% retained, better expectancy
        if variant == 'replay_only':
            return output(variant, net=(.5,)*9)  # too few even with strong expectancy
        if variant == 'both':
            return output(variant, net=(.09,)*12, equity=(1000., 950., 1050.))  # lower DD qualifies
        return output(variant)

    result = run_study(WINDOW, runner)
    assert [v for v, _ in calls] == ['no_filter', 'simulation_only', 'replay_only', 'both']
    assert all(w is WINDOW for _, w in calls)
    assert result['retention_criteria_passed'] == {'simulation_only': True, 'replay_only': False, 'both': True}
    assert result['filter_enabled'] is False and result['provenance_verified'] is False


def test_unchanged_results_and_empty_baseline_cannot_pass():
    assert not any(run_study(WINDOW, lambda v, w: output(v))['retention_criteria_passed'].values())
    assert not any(run_study(WINDOW, lambda v, w: output(v, net=()))['retention_criteria_passed'].values())


def test_holdout_overlap_prevents_any_run():
    calls = []
    result = run_study(replace(WINDOW, end=WINDOW.final_holdout_start.replace(day=2)),
                       lambda v, w: calls.append(v))
    assert result['complete'] is False and calls == []


@pytest.mark.parametrize('damage', ['hash', 'capital', 'nan', 'reused_id', 'variant', 'exception'])
def test_incomparable_or_failed_run_disables_entire_study(damage):
    def runner(variant, window):
        result = output(variant)
        if variant != 'both':
            return result
        if damage == 'hash':
            return replace(result, input_hash='different tape')
        if damage == 'capital':
            return replace(result, starting_capital=2000.)
        if damage == 'nan':
            return replace(result, net_r=(float('nan'),))
        if damage == 'reused_id':
            return replace(result, run_id='no_filter-independent-run')
        if damage == 'variant':
            return replace(result, variant='no_filter')
        raise RuntimeError('backtest failed')

    result = run_study(WINDOW, runner)
    assert result['complete'] is False
    assert not any(result['retention_criteria_passed'].values())
    assert result['filter_enabled'] is False
