"""Tests for the robustness suite: bootstrap, cost stress, multiple testing, attribution.

These are statistical helpers, so the tests pin the properties that matter: a losing
series must never be described as robust, more trials must lower the bar that has to be
cleared, and a fee-drag result must be reported as such.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.backtest.metrics import Metrics, TradeRecord
from tradebot.backtest.robustness import (
    Trial,
    attribution,
    block_bootstrap,
    bootstrap_drawdown,
    buy_and_hold,
    excess_over_benchmark,
    expected_max_sharpe,
    multiple_testing_report,
    robustness_report,
    stress_config,
    stressed_trade_pnls,
    trial_log_from_sensitivity,
)
from tradebot.core.money import D

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def make_trade(pnl: str, *, fees: str = "1", strategy: str = "S1", symbol: str = "BTC/USDT",
               reason: str = "stop", hour: int = 10) -> TradeRecord:
    return TradeRecord(
        symbol=symbol,
        strategy=strategy,
        pnl=D(pnl),
        r_multiple=D("1"),
        opened_at=T0,
        closed_at=T0 + timedelta(hours=hour),
        fees=D(fees),
        reason=reason,
    )


def make_curve(values: list[str]) -> list[tuple[datetime, Decimal]]:
    return [(T0 + timedelta(days=i), D(v)) for i, v in enumerate(values)]


class TestBootstrap:
    def test_a_positive_series_mostly_resamples_positive(self):
        result = block_bootstrap([0.01] * 40)
        assert result["p05"] == pytest.approx(0.4)
        assert result["negative_share"] == 0.0

    def test_a_negative_series_is_reported_as_negative(self):
        result = block_bootstrap([-0.01] * 40)
        assert result["p95"] < 0
        assert result["negative_share"] == 1.0

    def test_block_bootstrap_is_seeded(self):
        a = block_bootstrap([0.01, -0.02, 0.005, 0.0] * 10, seed=3)
        b = block_bootstrap([0.01, -0.02, 0.005, 0.0] * 10, seed=3)
        assert a == b

    def test_empty_series_returns_zeros(self):
        assert block_bootstrap([])["p50"] == 0.0

    def test_drawdown_bootstrap_finds_the_loss_path(self):
        returns = [-0.05, -0.04, 0.01, 0.01, -0.03, 0.02] * 5
        result = bootstrap_drawdown(returns)
        assert result["p95_drawdown"] < 0
        # the 5th percentile must be at least as bad as the median
        assert result["p95_drawdown"] <= result["p50_drawdown"]

    def test_a_strictly_rising_series_has_no_drawdown(self):
        assert bootstrap_drawdown([0.01] * 30)["p95_drawdown"] == 0.0


class TestCostStress:
    def test_stress_config_multiplies_fees_without_touching_the_original(self):
        from tradebot.config import load_config

        cfg = load_config()
        before = cfg.settings.paper.taker_fee
        stressed = stress_config(cfg, 2.0)
        assert stressed.settings.paper.taker_fee == before * 2
        assert cfg.settings.paper.taker_fee == before, "the original config must not be mutated"

    def test_stressed_pnl_charges_the_extra_fee(self):
        trades = [make_trade("10", fees="2"), make_trade("-4", fees="2")]
        stressed = stressed_trade_pnls(trades, 2.0)
        assert stressed == pytest.approx([8.0, -6.0])

    def test_stress_of_one_is_a_no_op(self):
        trades = [make_trade("10", fees="2")]
        assert stressed_trade_pnls(trades, 1.0) == pytest.approx([10.0])


class TestMultipleTesting:
    def test_expected_max_sharpe_grows_with_trials(self):
        assert expected_max_sharpe(1) == 0.0
        assert expected_max_sharpe(10) < expected_max_sharpe(100) < expected_max_sharpe(1000)

    def test_one_trial_of_modest_sharpe_is_not_deflated_away(self):
        report = multiple_testing_report([Trial("only", sharpe=4.0)], observations=500)
        assert report["deflated_sharpe"] > 0
        assert "survives" in report["verdict"]

    def test_many_trials_raise_the_bar(self):
        trials = [Trial(f"t{i}", sharpe=1.2) for i in range(200)]
        report = multiple_testing_report(trials, observations=250)
        assert "not distinguishable from luck" in report["verdict"]

    def test_a_losing_best_trial_is_called_out(self):
        report = multiple_testing_report([Trial("bad", sharpe=-1.0)], observations=100)
        assert "no edge" in report["verdict"]

    def test_no_trials_is_stated_plainly(self):
        assert "no trials" in multiple_testing_report([], observations=10)["verdict"]

    def test_sensitivity_cells_become_trials(self):
        cells = [{"ema_fast": 12, "score": 0.5}, {"ema_fast": 20, "score": -0.2}]
        trials = trial_log_from_sensitivity(cells)
        assert len(trials) == 2
        assert trials[0].params["ema_fast"] == 12


class TestBenchmark:
    def test_buy_and_hold_averages_symbols_and_charges_fees(self):
        result = buy_and_hold({"A": [100, 110], "B": [100, 90]}, fee_rate=0.0)
        assert result["return_pct"] == pytest.approx(0.0)  # +10% and -10%

    def test_a_single_instrument_matches_its_own_return(self):
        result = buy_and_hold({"A": [100, 120]}, fee_rate=0.0)
        assert result["return_pct"] == pytest.approx(20.0)

    def test_fees_are_deducted(self):
        with_fees = buy_and_hold({"A": [100, 120]}, fee_rate=0.01)["return_pct"]
        without = buy_and_hold({"A": [100, 120]}, fee_rate=0.0)["return_pct"]
        assert with_fees == pytest.approx(without - 2.0)

    def test_flat_prices_are_skipped(self):
        assert buy_and_hold({"A": [100]})["return_pct"] == 0.0

    def test_excess_is_reported_with_a_direction(self):
        better = excess_over_benchmark(5.0, 2.0)
        worse = excess_over_benchmark(-1.0, 8.0)
        assert "beat" in better["verdict"] and better["excess_pct"] == pytest.approx(3.0)
        assert "trailed" in worse["verdict"]


class TestAttribution:
    def test_pnl_is_broken_down_by_strategy_and_symbol(self):
        trades = [
            make_trade("10", strategy="S1", symbol="BTC/USDT"),
            make_trade("-4", strategy="S2", symbol="ETH/USDT"),
        ]
        report = attribution(trades)
        assert report["by_strategy"]["S1"]["pnl"] == 10.0
        assert report["by_strategy"]["S2"]["pnl"] == -4.0
        assert report["by_symbol"]["ETH/USDT"]["trades"] == 1
        assert report["net_pnl"] == 6.0

    def test_fee_drag_is_measured_against_gross(self):
        report = attribution([make_trade("10", fees="5")])
        assert report["fees"] == 5.0
        assert report["fee_drag_pct_of_gross"] == pytest.approx(50.0)

    def test_dependence_on_a_single_trade_is_flagged(self):
        trades = [make_trade("100"), make_trade("-10"), make_trade("-11")]
        report = attribution(trades)
        assert report["net_pnl"] == pytest.approx(79.0)
        assert report["pnl_without_best_trade"] == pytest.approx(-21.0)
        assert report["depends_on_one_trade"] is True

    def test_a_broad_edge_is_not_flagged(self):
        trades = [make_trade("5") for _ in range(10)]
        assert attribution(trades)["depends_on_one_trade"] is False


class TestReport:
    def test_a_losing_run_is_never_called_robust(self):
        curve = make_curve(["1000", "990", "975", "960"])
        trades = [make_trade("-15", fees="5") for _ in range(3)]
        report = robustness_report(curve=curve, trades=trades, starting_equity=D("1000"),
                                   iterations=200)
        assert report["checks"]["cost_stress"]["ok"] is False
        assert "fragile" in report["verdict"]

    def test_a_clean_run_passes_the_checks(self):
        curve = make_curve(["1000", "1005", "1010", "1020"])
        trades = [make_trade("5", fees="0.1") for _ in range(3)]
        report = robustness_report(curve=curve, trades=trades, starting_equity=D("1000"),
                                   iterations=200)
        assert report["checks"]["cost_stress"]["ok"] is True
        assert "robust" in report["verdict"]

    def test_thin_cost_margins_are_called_out(self):
        curve = make_curve(["1000", "1010"])
        trades = [make_trade("10", fees="9.9")]
        metrics = Metrics(
            total_return=D("0.0001"),
            sharpe=D("0.2"),
            sortino=D("0.2"),
            calmar=D("0.1"),
            max_drawdown=D("-0.001"),
            trade_count=1,
            win_rate=D("1"),
            avg_win=D("10"),
            avg_loss=D("0"),
            expectancy_r=D("0.1"),
            fees=D("9.9"),
        )
        report = robustness_report(
            curve=curve, trades=trades, starting_equity=D("1000"),
            cost_stress_metrics=metrics, iterations=50,
        )
        assert report["checks"]["cost_stress_rerun"]["ok"] is True
        assert report["checks"]["cost_stress_rerun"]["thin_margin"] is True
        assert "thin" in report["verdict"]

    def test_benchmark_and_attribution_are_included_when_given(self):
        curve = make_curve(["1000", "1020"])
        trades = [make_trade("20")]
        report = robustness_report(
            curve=curve, trades=trades, starting_equity=D("1000"),
            closes={"BTC/USDT": [100, 101]}, iterations=50,
        )
        assert report["benchmark"]["return_pct"] < 2.0
        assert "attribution" in report
        assert report["benchmark_comparison"]["excess_pct"] > 0

    def test_multiple_testing_is_included_when_trials_are_given(self):
        curve = make_curve(["1000", "1001"])
        report = robustness_report(
            curve=curve, trades=[], starting_equity=D("1000"),
            trials=[Trial("a", sharpe=1.0)], iterations=50,
        )
        assert report["multiple_testing"]["trials"] == 1
