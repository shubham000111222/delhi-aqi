"""Walk-forward cost stress runs separate windows with independently carried capital."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tradebot.backtest.walkforward import SegmentResult, rolling_windows, run_walk_forward
from tradebot.config import Mode
from tradebot.core.money import D

START = datetime(2024, 1, 1, tzinfo=UTC)


async def test_cost_rerun_uses_same_windows_and_parameters_but_separate_capital(cfg):
    windows = rolling_windows(START, START + timedelta(days=3), train_days=1, test_days=1)
    calls = []

    async def runner(config, start, end, *, capital, seed):
        factor = config.settings.paper.slippage_multiplier
        params = dict(config.strategies.strategies["S1_ema_trend"].params)
        calls.append((start, end, capital, seed, factor, params))
        growth = D("1.10") if factor == D("1") else D("0.90")
        return SegmentResult(start_equity=capital, end_equity=capital * growth)

    report = await run_walk_forward(cfg, windows, capital=D("1000"), seed=17, runner=runner, cost_stress=True)
    assert len(calls) == 4
    assert [entry[4] for entry in calls] == [D("1"), D("1"), D("2"), D("2")]
    assert [entry[2] for entry in calls] == [D("1000"), D("1100"), D("1000"), D("900")]
    assert [(entry[0], entry[1], entry[3], entry[5]) for entry in calls[:2]] == [
        (entry[0], entry[1], entry[3], entry[5]) for entry in calls[2:]]
    assert report["cost_stress"]["metrics"]["total_return"] == pytest.approx(-0.19)
    assert report["cost_stress"]["runner_verified"] is False
    assert not next(g for g in report["gates"] if g.name == "cost_stress_2x").passed
    assert not report["passed"]


@pytest.mark.parametrize("mode", [Mode.LIVE, Mode.TESTNET])
async def test_backtest_walk_forward_never_runs_with_venue_mode(cfg, mode):
    cfg = replace(cfg, settings=cfg.settings.model_copy(update={"mode": mode}))
    with pytest.raises(ValueError, match="paper mode"):
        await run_walk_forward(cfg, [], cost_stress=True)


async def test_real_engine_reruns_same_windows_and_remains_not_promotable(fast_cfg):
    windows = rolling_windows(START, START + timedelta(minutes=45), train_days=10 / 1440, test_days=10 / 1440)
    result = await run_walk_forward(fast_cfg, windows[:2], capital=D("1000"), seed=7, cost_stress=True)
    assert result["cost_stress"]["runner_verified"] is True
    assert len(result["cost_stress"]["windows"]) == len(result["windows"]) == 2
    assert result["cost_stress"]["venue_costs_verified"] is False
    assert not next(g for g in result["gates"] if g.name == "out_of_sample_provenance").passed
    assert not result["passed"]


async def test_robustness_reruns_and_remains_not_promotable(fast_cfg, monkeypatch):
    from tradebot import main

    monkeypatch.setattr(main, "_sim_start", lambda cfg: START)
    monkeypatch.setattr(main, "_sim_end", lambda cfg, start, days: start + timedelta(minutes=20))
    result = await main._run_robustness(fast_cfg, days=1, seed=7, multiplier=2.0)
    assert "venue costs" in result["stress_model"]
    assert "supplied stress return" in next(g for g in result["gates"] if g["name"] == "cost_stress_2x")["detail"]
    assert not result["passed"]


async def test_stress_uses_nominal_refit_parameters_not_a_second_refit(cfg):
    windows = rolling_windows(START, START + timedelta(days=3), train_days=1, test_days=1)
    calls = []
    train_calls = []

    async def runner(config, start, end, *, capital, seed):
        calls.append((start, config.settings.paper.slippage_multiplier,
                      config.strategies.strategies["S1_ema_trend"].params.get("ema_fast")))
        return SegmentResult(start_equity=capital, end_equity=capital)

    def train(window, result):
        train_calls.append(window.index)
        return {"S1_ema_trend": {"ema_fast": 10 + window.index}}

    report = await run_walk_forward(cfg, windows, capital=D("1000"), runner=runner,
                                    train_fn=train, cost_stress=True)
    assert train_calls == [0, 1]
    assert [c[2] for c in calls if c[1] == D("2")] == [10, 11]
    assert report["summary"]["parameters_refit"] is True
