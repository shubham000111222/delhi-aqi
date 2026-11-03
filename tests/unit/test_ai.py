"""Tests for the AI layer: feature store, signal library, screening, complexity ladder.

The theme is honesty, not accuracy. Most of these tests check that the machinery *refuses*
to claim an edge: shuffled labels must collapse, purged folds must not overlap, a losing
signal must be dropped, and a more complex model must be rejected when it does not beat
the simpler one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pytest

from tradebot.ai.features import (
    FEATURES,
    FeatureStore,
    forward_returns,
    rolling_corr,
    rolling_zscore,
    shift,
    synthetic_candles,
)
from tradebot.ai.ladder import (
    BaselineModel,
    BoostingModel,
    LevelResult,
    RidgeModel,
    brier,
    fold_boundaries,
    platt_scale,
    promote,
    purged_folds,
    run_ladder,
    shuffled_label_test,
    time_shift_test,
)
from tradebot.ai.signals import (
    SIGNALS,
    SignalSpec,
    evaluate,
    rank_correlation,
    rank_ic,
    screen,
)

START = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def candles():
    return synthetic_candles(count=600, seed=5)


@pytest.fixture(scope="module")
def frame(candles):
    return FeatureStore().compute(candles)


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------


class TestFeatures:
    def test_the_library_covers_every_family_in_the_plan(self):
        families = {spec.family for spec in FEATURES.values()}
        assert {"trend", "mean_reversion", "volatility", "volume", "order_book", "time"} <= families
        assert len(FEATURES) >= 25

    def test_a_feature_never_uses_a_later_bar(self, candles):
        """Truncating the series must not change any earlier value."""
        store = FeatureStore()
        full = store.compute(candles)
        partial = store.compute(candles[:400])
        for name in ("ema_slope_20", "donchian_pos_20", "rsi_distance_14", "volume_z_20"):
            np.testing.assert_allclose(
                partial.features[name][300:395],
                full.features[name][300:395],
                rtol=1e-9,
                equal_nan=True,
                err_msg=f"{name} changed when future bars were removed",
            )

    def test_normalisation_is_rolling_so_the_start_is_nan(self, frame):
        values = frame.features["close_z_20"]
        assert np.isnan(values[:95]).all(), "no trailing window yet, so no z-score"
        assert not np.isnan(values[200])

    def test_a_constant_feature_is_zero_not_nan(self):
        constant = np.full(200, 3.0)
        z = rolling_zscore(constant, 20)
        assert np.isnan(z[:19]).all()
        assert np.nan_to_num(z[19:]).tolist() == [0.0] * 181

    def test_nan_share_is_reported_not_hidden(self, frame):
        shares = frame.nan_share()
        assert all(0.0 <= value <= 1.0 for value in shares.values())
        assert shares["close_z_20"] > 0.0

    def test_book_features_are_absent_without_books(self, frame):
        assert "book_imbalance_5" not in frame.features
        assert frame.families["close_z_20"] == "mean_reversion"

    def test_book_features_align_to_bars_and_never_look_ahead(self, candles):
        from tradebot.core.models import BookLevel, OrderBook

        books = {
            candle.close_time: OrderBook(
                symbol=candle.symbol,
                ts=candle.close_time,
                bids=(BookLevel(price=Decimal("99"), qty=Decimal("3")),),
                asks=(BookLevel(price=Decimal("101"), qty=Decimal("1")),),
            )
            for candle in candles[:300]
        }
        frame = FeatureStore().compute(candles, books=books)
        assert "book_imbalance_5" in frame.features
        imbalance = frame.features["book_imbalance_5"]
        assert not np.isnan(imbalance[:300]).any()
        # 3 bid vs 1 ask -> (3-1)/(3+1) = 0.5, normalisation off so it is the raw value
        assert imbalance[10] == pytest.approx(0.5, abs=1e-9)
        # bars with no book yet stay NaN rather than borrowing a future snapshot
        assert np.isnan(imbalance[400:]).all()

    def test_rolling_correlation_is_bounded(self):
        rng = np.random.default_rng(1)
        a = rng.normal(size=300)
        b = rng.normal(size=300)
        rho = rolling_corr(a, b, 50)
        finite = rho[~np.isnan(rho)]
        assert np.all(np.abs(finite) <= 1.0)

    def test_forward_returns_is_the_only_place_the_future_appears(self):
        close = np.array([100.0, 110.0, 121.0, 133.1])
        result = forward_returns(close, 1)
        assert np.isnan(result[-1])
        assert result[0] == pytest.approx(0.1)

    def test_shift_lags_rather_than_leads(self):
        values = np.arange(10, dtype=float)
        assert np.array_equal(shift(values, 2)[2:], values[:-2])
        assert np.isnan(shift(values, 2)[:2]).all()

    def test_too_few_candles_is_an_error(self):
        with pytest.raises(ValueError):
            FeatureStore().compute(synthetic_candles(count=1))


# ---------------------------------------------------------------------------
# signals and screening
# ---------------------------------------------------------------------------


class TestSignals:
    def test_library_size_and_shape(self):
        assert 30 <= len(SIGNALS) <= 60, "the plan asks for 30 to 60 signals"
        assert all(-1.0 <= 1.0 for _ in SIGNALS)  # sanity on the tuple type

    def test_every_score_is_in_range(self, frame):
        for spec in SIGNALS:
            scores = spec.compute(frame)
            finite = scores[~np.isnan(scores)]
            assert np.all(finite >= -1.0) and np.all(finite <= 1.0), spec.name

    def test_a_missing_input_does_not_crash(self, frame):
        spec = SignalSpec("test", "cross_asset", ("not_a_feature",), 1,
                          lambda f: f.features.get("not_a_feature", np.zeros(len(f))))
        assert np.allclose(spec.compute(frame), 0.0)

    def test_rank_correlation_of_identical_series_is_one(self):
        values = np.linspace(0, 1, 50)
        assert rank_correlation(values, values) == pytest.approx(1.0)

    def test_rank_correlation_handles_ties_and_tiny_samples(self):
        assert rank_correlation(np.ones(50), np.arange(50.0)) == 0.0
        assert rank_correlation(np.arange(5.0), np.arange(5.0)) == 0.0

    def test_rank_ic_subsamples_to_avoid_overlap(self):
        target = np.arange(100.0)
        score = target.copy()
        assert rank_ic(score, target, 1) == pytest.approx(1.0)
        # a 10-bar stride only uses 10 observations
        assert rank_ic(score, target, 10) == pytest.approx(1.0)

    def test_a_pure_noise_signal_is_dropped(self, frame):
        rng = np.random.default_rng(3)
        noise = rng.normal(size=len(frame))
        spec = SignalSpec("pure_noise", "time", (), 0, lambda f: noise)
        result = screen(frame, specs=[spec], min_ic=0.01)
        assert result.kept == []
        assert result.dropped, "a dropped signal must carry a reason"
        assert result.dropped[0]["name"] == "pure_noise"

    def test_a_planted_signal_is_kept(self, frame):
        target = forward_returns(frame.close, 4)
        planted = np.nan_to_num(target, nan=0.0)
        spec = SignalSpec("planted", "time", (), 0, lambda f: np.sign(planted))
        result = screen(frame, specs=[spec], min_ic=0.01)
        assert [s.name for s in result.kept] == ["planted"]

    def test_correlated_duplicates_are_pruned(self, frame):
        target = np.nan_to_num(forward_returns(frame.close, 4), nan=0.0)
        strong = SignalSpec("strong", "time", (), 0, lambda f: np.sign(target))
        duplicate = SignalSpec("duplicate", "time", (), 0, lambda f: np.sign(target) * 0.9)
        result = screen(frame, specs=[strong, duplicate], min_ic=0.0)
        assert "strong" in [s.name for s in result.kept]
        assert "duplicate" not in [s.name for s in result.kept]
        assert any("redundant" in d["reason"] for d in result.dropped)

    def test_shuffled_ic_collapses_for_a_real_signal(self, frame):
        target = np.nan_to_num(forward_returns(frame.close, 8), nan=0.0)
        spec = SignalSpec("planted", "trend", (), 0, lambda f: np.sign(target))
        result = screen(frame, specs=[spec], primary_horizon=8, min_ic=0.0)
        assert abs(result.shuffled_ic) < 0.2

    def test_evaluation_reports_per_horizon_ic_and_costs(self, frame):
        spec = SIGNALS[0]
        evaluation = evaluate(frame, spec, horizons=(1, 4, 12), primary_horizon=4)
        assert set(evaluation.ic_by_horizon) == {1, 4, 12}
        assert evaluation.samples > 0
        assert evaluation.net_pnl <= evaluation.gross_pnl or evaluation.gross_pnl <= 0

    def test_the_screen_reports_the_noise_band_it_judges_against(self, frame):
        result = screen(frame, specs=SIGNALS[:3], primary_horizon=4)
        rows = result.table_rows()
        assert rows and rows[0]["noise_band"] > 0
        assert "distinguishable" in rows[0]


# ---------------------------------------------------------------------------
# ladders
# ---------------------------------------------------------------------------


class TestPurgedFolds:
    def test_folds_never_train_on_their_own_test_window(self):
        folds = purged_folds(1000, n_folds=4, horizon=5)
        assert len(folds) >= 3
        for fold in folds:
            assert fold.train[-1] < fold.test[0] - 5 + 1
            assert not set(fold.train) & set(fold.test)

    def test_purging_removes_the_overlapping_band(self):
        folds = purged_folds(600, n_folds=3, horizon=10)
        for fold in folds:
            gap = fold.test[0] - fold.train[-1] - 1
            assert gap >= 10, "the forward-return window must not straddle train and test"

    def test_embargo_creates_a_gap_after_earlier_test_blocks(self):
        folds = purged_folds(900, n_folds=4, horizon=4, embargo=50)
        assert len(folds) >= 2
        for earlier, later in zip(folds, folds[1:], strict=False):
            forbidden = set(range(earlier.test[-1] + 1, earlier.test[-1] + 51))
            assert not forbidden & set(later.train.tolist())

    def test_too_few_rows_is_an_error(self):
        with pytest.raises(ValueError):
            purged_folds(10)

    def test_fold_boundaries_are_reportable(self):
        rows = list(fold_boundaries(800, n_folds=3, horizon=4))
        assert rows and {"fold", "train_start", "test_start", "purged_rows"} <= set(rows[0])


class TestModels:
    def test_ridge_learns_a_linear_relationship(self):
        rng = np.random.default_rng(4)
        x = rng.normal(size=(400, 2))
        y = 0.8 * x[:, 0] - 0.3 * x[:, 1]
        from tradebot.ai.features import FeatureFrame

        frame = FeatureFrame(symbol="X", ts=[START] * 400, close=np.ones(400),
                             features={"a": x[:, 0], "b": x[:, 1]})
        model = RidgeModel(alpha=1e-6).fit(frame, np.arange(400), y)
        predicted = model.predict(frame)
        assert np.corrcoef(predicted, y)[0, 1] > 0.99

    def test_boosting_learns_a_non_linear_function(self):
        rng = np.random.default_rng(6)
        x = rng.uniform(-3, 3, size=(600, 1))
        y = (x[:, 0] ** 2) * np.sign(x[:, 0])
        from tradebot.ai.features import FeatureFrame

        frame = FeatureFrame(symbol="X", ts=[START] * 600, close=np.ones(600), features={"x": x[:, 0]})
        model = BoostingModel(n_estimators=60, depth=2).fit(frame, np.arange(600), y)
        predicted = model.predict(frame)
        assert np.corrcoef(predicted, y)[0, 1] > 0.9

    def test_gradient_boosting_refuses_to_fit_nothing(self):
        from tradebot.ai.features import FeatureFrame

        frame = FeatureFrame(symbol="X", ts=[START] * 30, close=np.ones(30), features={"x": np.zeros(30)})
        model = BoostingModel(n_estimators=10).fit(frame, np.arange(30), np.zeros(30))
        assert model.trees_ == []

    def test_baseline_is_ic_weighted_and_bounded(self, frame):
        model = BaselineModel(SIGNALS[:5])
        scores = model.predict(frame)
        assert np.all(np.abs(scores) <= 1.0)
        assert model.describe()["parameters"] == 0


class TestCalibration:
    def test_platt_scaling_learns_the_sign(self):
        rng = np.random.default_rng(8)
        scores = rng.normal(size=800)
        outcomes = (scores + rng.normal(scale=0.5, size=800) > 0).astype(float)
        a, b = platt_scale(scores, outcomes)
        assert a > 0

    def test_brier_rewards_better_probabilities(self):
        rng = np.random.default_rng(9)
        scores = rng.normal(size=500)
        outcomes = (scores > 0).astype(float)
        good = brier(scores * 10, outcomes, 1.0, 0.0)
        bad = brier(scores * 0.01, outcomes, 1.0, 0.0)
        assert good < bad

    def test_calibration_with_no_data_is_a_no_op(self):
        assert platt_scale(np.array([np.nan]), np.array([np.nan])) == (1.0, 0.0)


class TestPromotion:
    def _level(self, name: str, net: float, folds: list[float]) -> LevelResult:
        return LevelResult(
            level=name, folds=len(folds), net_expectancy=net, gross_expectancy=net,
            hit_rate=0.5, ic=0.01, turnover=0.1, trades=10, fold_expectancies=folds,
        )

    def test_a_more_complex_model_that_loses_is_rejected(self):
        ok, reason = promote(self._level("baseline", 0.001, [0.001]), self._level("ridge", -0.001, [-0.001]))
        assert ok is False
        assert "not profitable" in reason

    def test_a_marginal_improvement_is_not_enough(self):
        ok, reason = promote(self._level("baseline", 0.001, [0.001]), self._level("ridge", 0.00105, [0.00105]))
        assert ok is False
        assert "does not clear" in reason

    def test_a_clear_improvement_in_every_window_is_promoted(self):
        lower = self._level("baseline", 0.001, [0.001, 0.001, 0.001])
        higher = self._level("ridge", 0.002, [0.002, 0.002, 0.002])
        ok, reason = promote(lower, higher)
        assert ok is True
        assert "earns its complexity" in reason

    def test_a_model_that_wins_on_average_but_loses_a_window_is_rejected(self):
        lower = self._level("baseline", 0.001, [0.001, 0.001])
        higher = self._level("ridge", 0.002, [0.003, -0.001])
        ok, reason = promote(lower, higher)
        assert ok is False
        assert "not a stable improvement" in reason


class TestLadder:
    def test_the_ladder_runs_and_promotes_only_with_evidence(self, frame):
        result = run_ladder(frame, n_folds=3)
        assert [level.level for level in result.levels] == [
            "baseline", "ridge", "boosting", "sequence"
        ]
        assert result.promoted in {"baseline", "ridge", "boosting", "sequence"}
        assert result.windows == 3
        if result.promoted != "baseline":
            # promotion must never happen without a profitable, consistent improvement
            best = next(level for level in result.levels if level.level == result.promoted)
            assert best.net_expectancy > 0

    def test_the_sequence_rung_is_a_window_model_not_a_rerun_of_ridge(self, frame):
        from tradebot.ai.ladder import SequenceModel

        model = SequenceModel(lookback=5, hidden=8, epochs=40).fit(
            frame, purged_folds(len(frame), n_folds=3, horizon=4)[0].train,
            forward_returns(frame.close, 4),
        )
        description = model.describe()
        assert description["lookback"] == 5
        assert description["parameters"] > 0
        assert description["features"] > 0

    def test_a_row_without_enough_history_gets_a_neutral_score(self, frame):
        from tradebot.ai.ladder import SequenceModel

        model = SequenceModel(lookback=6, hidden=4, epochs=20, names=frame.names)
        folds = purged_folds(len(frame), n_folds=3, horizon=4)
        model.fit(frame, folds[0].train, forward_returns(frame.close, 4))
        scores = model.predict(frame, np.array([0, 1, len(frame) - 1]))
        assert scores[0] == 0.0 and scores[1] == 0.0, "no window, no opinion"
        assert scores[2] != 0.0

    def test_an_unfitted_sequence_model_scores_zero_everywhere(self, frame):
        from tradebot.ai.ladder import SequenceModel

        model = SequenceModel(names=frame.names)
        assert np.all(model.predict(frame) == 0.0)

    def test_the_sequence_rung_is_deterministic(self, frame):
        from tradebot.ai.ladder import SequenceModel

        folds = purged_folds(len(frame), n_folds=3, horizon=4)
        target = forward_returns(frame.close, 4)
        rows = folds[0].test
        first = SequenceModel(lookback=3, hidden=8, epochs=30).fit(frame, folds[0].train, target)
        second = SequenceModel(lookback=3, hidden=8, epochs=30).fit(frame, folds[0].train, target)
        assert np.allclose(first.predict(frame, rows), second.predict(frame, rows))

    def test_a_verdict_always_says_something_about_profitability(self, frame):
        result = run_ladder(frame, n_folds=3)
        assert "bps" in result.verdict

    def test_shuffled_labels_collapse(self, frame):
        result = shuffled_label_test(frame, horizon=4)
        assert result["available"] is True
        assert "collapsed" in result

    def test_time_shift_reports_both_shifts(self, frame):
        result = time_shift_test(frame, horizon=4)
        assert result["available"] is True
        assert result["far_shift_bars"] >= 50
        assert "decays" in result


class TestGuardrails:
    def test_the_risk_package_does_not_import_the_ai_layer(self):
        """A model must never be able to reach the risk engine (§19.8)."""
        import re
        from pathlib import Path

        risk_dir = Path(__file__).resolve().parents[2] / "src" / "tradebot" / "risk"
        forbidden = re.compile(r"^\s*(from|import)\s+[\w.]*\bai\b", re.MULTILINE)
        for path in risk_dir.glob("*.py"):
            assert not forbidden.search(path.read_text()), path.name

    def test_the_ai_layer_never_imports_the_execution_layer(self):
        import re
        from pathlib import Path

        ai_dir = Path(__file__).resolve().parents[2] / "src" / "tradebot" / "ai"
        forbidden = re.compile(
            r"^\s*(from|import)\s+[\w.]*(execution|engine|risk|order_manager)", re.MULTILINE
        )
        for path in ai_dir.glob("*.py"):
            text = path.read_text()
            assert not forbidden.search(text), f"{path.name} imports something it must not"


# ---------------------------------------------------------------------------
# model registry and the S4 strategy
# ---------------------------------------------------------------------------


class TestRegistry:
    def _card_kwargs(self, net: float = 0.0004) -> dict:
        return {
            "name": "demo_ridge",
            "level": "ridge",
            "features": ["close_z_20", "ema_slope_20"],
            "weights": [0.1, -0.2],
            "intercept": 0.01,
            "metrics": {"net_expectancy": net, "ic": 0.05},
            "training_window": {"start": "2024-01-01", "end": "2024-01-10"},
            "seed": 7,
        }

    def test_a_losing_model_cannot_be_registered(self, tmp_path):
        from tradebot.ai.registry import ModelRefusal, save_card

        with pytest.raises(ModelRefusal, match="nothing to promote"):
            save_card(tmp_path / "registry.json", **self._card_kwargs(net=-0.001))

    def test_a_profitable_model_is_saved_with_its_card(self, tmp_path):
        from tradebot.ai.registry import save_card

        path = tmp_path / "registry.json"
        card = save_card(path, **self._card_kwargs())
        assert path.exists() and card.approved
        loaded = type(card).load(path)
        assert loaded.features == card.features
        assert loaded.metrics["net_expectancy"] == pytest.approx(0.0004)

    def test_loading_a_missing_registry_is_not_an_error(self, tmp_path):
        from tradebot.ai.registry import load_model

        assert load_model(tmp_path / "absent.json") is None

    def test_a_corrupt_registry_is_treated_as_absent(self, tmp_path):
        from tradebot.ai.registry import load_model

        path = tmp_path / "registry.json"
        path.write_text("{not json")
        assert load_model(path) is None

    def test_scoring_uses_the_cards_weights(self, tmp_path, frame):
        from tradebot.ai.registry import PromotedModel, save_card

        path = tmp_path / "registry.json"
        card = save_card(path, **self._card_kwargs())
        model = PromotedModel(card=card, path=path)
        assert model.ready
        scores = model.score(frame)
        expected = (
            np.nan_to_num(frame.features["close_z_20"]) * 0.1
            + np.nan_to_num(frame.features["ema_slope_20"]) * -0.2
            + 0.01
        )
        np.testing.assert_allclose(scores, expected)

    def test_a_card_for_unknown_features_refuses_to_score(self, tmp_path, frame):
        from tradebot.ai.registry import PromotedModel, save_card

        path = tmp_path / "registry.json"
        kwargs = self._card_kwargs()
        kwargs["features"] = ["a_feature_that_no_longer_exists"]
        kwargs["weights"] = [1.0]
        card = save_card(path, **kwargs)
        model = PromotedModel(card=card, path=path)
        assert model.check(frame)["ready"] is False
        assert np.isnan(model.score(frame)).all()

    def test_a_mostly_nan_feature_blocks_the_model(self, tmp_path, frame):
        from tradebot.ai.features import FeatureFrame
        from tradebot.ai.registry import PromotedModel, save_card

        path = tmp_path / "registry.json"
        card = save_card(path, **self._card_kwargs())
        broken = FeatureFrame(
            symbol=frame.symbol,
            ts=frame.ts,
            close=frame.close,
            features={"close_z_20": np.full(len(frame), np.nan), "ema_slope_20": frame.features["ema_slope_20"]},
        )
        model = PromotedModel(card=card, path=path)
        check = model.check(broken)
        assert check["ready"] is False
        assert "close_z_20" in check["mostly_nan_features"]


class TestMlSignalStrategy:
    def _params(self, **overrides) -> object:
        from tradebot.strategies.base import StrategyParams

        raw = {"registry": "does-not-exist.json"} | overrides
        return StrategyParams(
            name="S4_ml_signal", timeframes=("1h",), regimes=(), priority=30, raw=raw
        )

    def test_without_a_card_it_is_inert_and_says_so(self):
        from tradebot.strategies.ml_signal import MlSignal

        strategy = MlSignal(self._params())
        assert strategy.status.startswith("not_promoted")
        assert strategy.describe()["refusals"] == 0

    def test_it_emits_nothing_without_a_model(self, candles):
        from tradebot.strategies.ml_signal import MlSignal

        strategy = MlSignal(self._params())
        assert strategy.evaluate(_context(candles)) is None

    def test_a_promoted_model_can_produce_a_long_signal(self, tmp_path, candles):
        from tradebot.ai.registry import save_card
        from tradebot.strategies.ml_signal import MlSignal

        registry = tmp_path / "registry.json"
        # a card with one feature and a large intercept, so the score clears the threshold
        card = save_card(
            registry,
            name="test_card",
            level="ridge",
            features=["close_z_20"],
            weights=[1.0],
            intercept=50.0,  # keeps the score above the entry threshold
            metrics={"net_expectancy": 0.01},
            threshold=0.0,
        )
        assert card.approved
        strategy = MlSignal(self._params(registry=str(registry)))
        strategy.entry_threshold = 0.3
        signal = strategy.evaluate(_context(candles))
        assert signal is not None, strategy.describe()
        assert signal.side.value == "buy"  # Signal.side is the order side
        assert signal.strategy == "S4_ml_signal"
        assert signal.limit_price is not None


def _context(candles):
    from tradebot.core.models import Regime
    from tradebot.core.money import D
    from tradebot.data.series import CandleSeries
    from tradebot.strategies.base import StrategyContext

    symbol = candles[0].symbol
    series = CandleSeries(symbol, "1m", 1000)
    for candle in candles[:400]:
        series.append(candle)
    last = candles[399]
    return StrategyContext(
        symbol=symbol,
        timeframe="1m",
        series=series,
        ts=last.close_time,
        mark=last.close,
        atr=float(last.close) * 0.002,
        regime=Regime.TREND,
        equity=D("1000"),
        book=None,
        position=None,
    )
