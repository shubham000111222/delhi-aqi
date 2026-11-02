"""Tests for meta-labelling: triple-barrier labels, the gate, and its refusals."""

from __future__ import annotations

import numpy as np
import pytest

from tradebot.ai.features import FeatureStore, synthetic_candles
from tradebot.ai.meta import (
    LogisticGate,
    MetaGate,
    auc_score,
    choose_threshold,
    evaluate_meta,
    save_meta,
    triple_barrier,
)


def arrays(candles):
    close = np.array([float(c.close) for c in candles])
    high = np.array([float(c.high) for c in candles])
    low = np.array([float(c.low) for c in candles])
    return close, high, low


class TestTripleBarrier:
    def test_a_rising_series_hits_the_target(self):
        close = np.linspace(100, 140, 80)
        candidates = np.zeros(80, dtype=bool)
        candidates[25] = True  # after the ATR warm-up
        setups = triple_barrier(close, close + 0.5, close - 0.5, candidates=candidates,
                                target_atr=1.0, stop_atr=1.0, max_bars=20)
        assert setups and setups[0].label == 1
        assert setups[0].held_bars > 0

    def test_a_falling_series_hits_the_stop(self):
        close = np.linspace(140, 100, 80)
        candidates = np.zeros(80, dtype=bool)
        candidates[25] = True
        setups = triple_barrier(close, close + 0.5, close - 0.5, candidates=candidates,
                                target_atr=1.0, stop_atr=1.0, max_bars=20)
        assert setups and setups[0].label == 0

    def test_a_bar_that_breaches_both_barriers_counts_as_a_stop(self):
        close = np.full(60, 100.0)
        high = close + 0.5
        low = close - 0.5
        # one bar spans both barriers
        high[30] = 200.0
        low[30] = 1.0
        candidates = np.zeros(60, dtype=bool)
        candidates[25] = True
        setups = triple_barrier(close, high, low, candidates=candidates,
                                target_atr=1.0, stop_atr=1.0, max_bars=10)
        assert setups[0].label == 0, "an ambiguous bar must be read pessimistically"

    def test_no_candidates_means_no_setups(self):
        close = np.linspace(100, 120, 50)
        assert triple_barrier(close, close, close, candidates=np.zeros(50, dtype=bool)) == []

    def test_the_vertical_barrier_closes_the_trade(self):
        close = np.full(80, 100.0)  # nothing moves: neither barrier is touched
        high = close + 0.5
        low = close - 0.5
        candidates = np.zeros(80, dtype=bool)
        candidates[25] = True
        setups = triple_barrier(close, high, low, candidates=candidates,
                                target_atr=3.0, stop_atr=3.0, max_bars=10)
        assert setups[0].held_bars == 10
        assert setups[0].label == 0

    def test_the_last_bars_have_no_room_to_resolve(self):
        close = np.linspace(100, 120, 60)
        candidates = np.zeros(60, dtype=bool)
        candidates[59] = True
        assert triple_barrier(close, close + 0.5, close - 0.5, candidates=candidates) == []

    def test_a_candidate_before_the_warm_up_is_skipped(self):
        close = np.linspace(100, 120, 60)
        candidates = np.zeros(60, dtype=bool)
        candidates[3] = True  # ATR is still NaN here
        assert triple_barrier(close, close + 0.5, close - 0.5, candidates=candidates) == []


class TestAuc:
    def test_a_perfect_ranking_scores_one(self):
        assert auc_score(np.array([0.1, 0.2, 0.8, 0.9]), np.array([0, 0, 1, 1])) == 1.0

    def test_an_inverted_ranking_scores_zero(self):
        assert auc_score(np.array([0.9, 0.8, 0.2, 0.1]), np.array([0, 0, 1, 1])) == 0.0

    def test_one_class_is_a_coin_flip(self):
        assert auc_score(np.array([0.1, 0.9]), np.array([1, 1])) == 0.5


class TestThreshold:
    def test_the_threshold_chases_expectancy_not_accuracy(self):
        probabilities = np.array([0.1, 0.2, 0.3, 0.7, 0.8])
        # the high-probability trades are the losers
        outcomes = np.array([0.05, 0.05, 0.05, -0.10, -0.10])
        threshold = choose_threshold(probabilities, outcomes, cost_per_trade=0.0)
        assert threshold <= 0.5, "a sane selector avoids the losing tail"

    def test_cost_per_trade_raises_the_bar(self):
        probabilities = np.array([0.2, 0.4, 0.6, 0.8])
        outcomes = np.array([0.001, 0.002, 0.003, 0.004])
        free = choose_threshold(probabilities, outcomes, cost_per_trade=0.0)
        costly = choose_threshold(probabilities, outcomes, cost_per_trade=0.0035)
        assert costly >= free


class TestLogisticGate:
    def test_it_learns_a_separable_problem(self):
        rng = np.random.default_rng(2)
        x = rng.normal(size=(600, 2))
        y = (x[:, 0] + x[:, 1] > 0).astype(float)
        gate = LogisticGate(alpha=0.1).fit(x, y)
        probabilities = gate.predict_proba(x)
        assert auc_score(probabilities, y) > 0.9

    def test_one_class_does_not_produce_a_model(self):
        x = np.random.default_rng(3).normal(size=(100, 2))
        gate = LogisticGate().fit(x, np.ones(100))
        assert gate.ready is False
        assert np.isnan(gate.predict_proba(x)).all()


class TestEvaluateMeta:
    def _frame(self, count: int = 1200):
        candles = synthetic_candles(count=count, seed=4)
        return candles, FeatureStore().compute(candles)

    def test_a_random_signal_gets_an_honest_no(self):
        candles, frame = self._frame()
        candidates = np.nan_to_num(frame.features["ema_slope_20"]) > 0.3
        report, model, threshold = evaluate_meta(frame, candidates=candidates, max_bars=30, n_folds=3)
        assert report.setups > 40
        assert 0.0 <= report.auc <= 1.0
        assert "costs" in report.verdict or "gate" in report.verdict
        assert model.ready in (True, False)

    def test_too_few_candidates_is_reported_not_fitted(self):
        candles, frame = self._frame(400)
        candidates = np.zeros(len(frame), dtype=bool)
        candidates[10] = True
        report, model, threshold = evaluate_meta(frame, candidates=candidates)
        assert report.setups <= 40
        assert "too few" in report.verdict
        assert model.ready is False

    def test_saving_is_refused_when_the_gate_loses(self, tmp_path):
        candles, frame = self._frame()
        candidates = np.nan_to_num(frame.features["ema_slope_20"]) > 0.3
        report, model, threshold = evaluate_meta(frame, candidates=candidates, max_bars=30, n_folds=3)
        if model.ready and report.expectancy_taken <= 0:
            with pytest.raises(ValueError, match="do not make money"):
                save_meta(tmp_path / "meta.json", name="m", model=model,
                          features=frame.names, threshold=threshold, report=report)
        else:
            pytest.skip("this sample happened to gate profitably")


class TestRuntimeGate:
    def test_without_a_model_it_refuses(self, ):
        frame = FeatureStore().compute(synthetic_candles(count=300, seed=1))
        gate = MetaGate(model=LogisticGate(), threshold=0.6, features=["close_z_20"])
        decision = gate.decide(frame)
        assert decision.take is False
        assert "no model" in decision.reason

    def test_missing_features_refuse_rather_than_guess(self):
        frame = FeatureStore().compute(synthetic_candles(count=300, seed=1))
        model = LogisticGate(alpha=0.1).fit(
            np.random.default_rng(1).normal(size=(100, 1)),
            (np.arange(100) % 2).astype(float),
        )
        gate = MetaGate(model=model, threshold=0.5, features=["a_feature_that_is_gone"])
        assert gate.decide(frame).take is False
        assert "missing features" in gate.decide(frame).reason

    def test_expected_r_is_consistent_with_the_probability(self):
        from tradebot.ai.meta import MetaLabelDecision

        decision = MetaLabelDecision(True, 0.8, 0.5, "ok", expected_r=0.8 * 1.5 - 0.2)
        assert decision.as_dict()["expected_r"] == pytest.approx(1.0)
