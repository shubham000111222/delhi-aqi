"""The complexity ladder (§19.2): baseline, ridge, gradient boosting - in that order.

Each rung has to *earn* promotion over the rung below, out of sample, across at least
three walk-forward windows, by a clear margin (10% net expectancy). If it does not, the
simpler model wins. That rule is enforced in code (`promote`) rather than left to
judgement, because judgement is exactly what a search over many models corrupts.

Why the fold machinery matters more than the models:

* **Purging** - training rows whose forward-return window overlaps the test window are
  dropped. Otherwise the model trains on the answer it is about to be tested on.
* **Embargo** - a gap after each test block, because the next training rows are still
  contaminated by the same underlying move.
* **Weekly retrain on a rolling window** - what production would do, simulated here.

The boosting implementation is deliberately small (depth-limited squared-loss trees with
shrinkage and early stopping). It is a reference rung, not a LightGBM replacement; the
docstring says so, and `LADDER_LEVELS` records what is not yet implemented so nobody
mistakes this for a finished research stack.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .features import Array, FeatureFrame, forward_returns
from .signals import SIGNALS, SignalSpec, ic_weighted_score, pearson, rank_correlation

EPS = 1e-12

#: Rung 4 (neural/sequence models) is not here on purpose: the plan only allows it if it
#: beats rung 3 net of costs, and there is no evidence for that yet.
LADDER_LEVELS = ("baseline", "ridge", "boosting")


# ---------------------------------------------------------------------------
# folds
# ---------------------------------------------------------------------------


@dataclass
class Fold:
    train: Array
    test: Array
    index: int


def purged_folds(
    n_samples: int,
    *,
    n_folds: int = 5,
    horizon: int = 4,
    embargo: int | None = None,
    min_train: int | None = None,
) -> list[Fold]:
    """Purged, embargoed walk-forward splits.

    Test blocks are contiguous and always come *after* their training data, so this is a
    walk-forward and not shuffled k-fold. Two bands are removed from training:

    * **purge** - the ``horizon`` rows immediately before a test block, whose forward
      return window would overlap the test period;
    * **embargo** - the ``embargo`` rows immediately *after* each earlier test block, for
      the same reason in reverse. Without it, the next fold trains on rows whose labels
      were still being formed during the previous fold's test period.

    Both are enforced here rather than trusted to the caller, because this is the single
    easiest place for a backtest to lie.
    """
    if n_samples < 50:
        raise ValueError("need at least 50 samples to build folds")
    gap = horizon if embargo is None else max(0, embargo)
    min_train = min_train if min_train is not None else max(40, n_samples // 4)
    block = max(1, (n_samples - min_train) // n_folds)
    folds: list[Fold] = []
    earlier_test_ends: list[int] = []
    for index in range(n_folds):
        # the first test block starts *after* the purge band, otherwise `train_end` lands
        # one row short of `min_train` and the fold is silently dropped
        test_start = min_train + horizon + index * block
        test_end = min(n_samples, test_start + block)
        if test_start >= test_end:
            break
        train_end = test_start - horizon  # purge the overlap
        if train_end < min_train:
            continue
        keep = np.ones(train_end, dtype=bool)
        for previous_end in earlier_test_ends:
            embargo_start = previous_end
            embargo_stop = min(train_end, previous_end + gap)
            if embargo_start < embargo_stop:
                keep[embargo_start:embargo_stop] = False
        train = np.flatnonzero(keep)
        if train.size < min_train:
            earlier_test_ends.append(test_end)
            continue
        folds.append(Fold(train=train, test=np.arange(test_start, test_end), index=index))
        earlier_test_ends.append(test_end)
    return folds


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


class BaselineModel:
    """Level 1: IC-weighted average of the screened signals. No fitting at all."""

    level = "baseline"

    def __init__(self, specs: Sequence[SignalSpec] | None = None) -> None:
        self.specs = list(specs or SIGNALS)

    def fit(self, frame: FeatureFrame, train: Array, target: Array) -> BaselineModel:
        del frame, train, target
        return self

    def predict(self, frame: FeatureFrame, rows: Array | None = None) -> Array:
        score = ic_weighted_score(frame, self.specs)
        return score if rows is None else score[rows]

    def describe(self) -> dict[str, Any]:
        return {"level": self.level, "signals": len(self.specs), "parameters": 0}


class RidgeModel:
    """Level 2: ridge regression on the standardised feature matrix."""

    level = "ridge"

    def __init__(self, alpha: float = 1.0, names: Sequence[str] | None = None) -> None:
        self.alpha = alpha
        self.names = list(names) if names else None
        self.coef_: Array | None = None
        self.mean_: Array | None = None
        self.scale_: Array | None = None

    def _matrix(self, frame: FeatureFrame) -> Array:
        names = self.names or frame.names
        matrix = frame.matrix(names)
        if self.mean_ is None or self.scale_ is None:
            return np.nan_to_num(matrix, nan=0.0)
        return np.nan_to_num((matrix - self.mean_) / self.scale_, nan=0.0)

    def fit(self, frame: FeatureFrame, train: Array, target: Array) -> RidgeModel:
        self.names = list(self.names or frame.names)
        raw = frame.matrix(self.names)[train]
        usable = ~np.isnan(raw).any(axis=1) & ~np.isnan(target[train])
        rows = train[usable]
        if rows.size < 10:
            return self
        matrix = frame.matrix(self.names)[rows]
        y = target[rows]
        self.mean_ = matrix.mean(axis=0)
        self.scale_ = matrix.std(axis=0)
        self.scale_[self.scale_ <= EPS] = 1.0
        z = (matrix - self.mean_) / self.scale_
        design = np.column_stack([np.ones(len(z)), z])
        gram = design.T @ design
        penalty = np.eye(gram.shape[0]) * self.alpha
        penalty[0, 0] = 0.0  # never penalise the intercept
        self.coef_ = np.linalg.solve(gram + penalty, design.T @ y)
        return self

    def predict(self, frame: FeatureFrame, rows: Array | None = None) -> Array:
        matrix = self._matrix(frame)
        if self.coef_ is None:
            # unfitted: zero everywhere, and the *same shape* the caller asked for
            zeros = np.zeros(len(matrix))
            return zeros if rows is None else zeros[rows]
        design = np.column_stack([np.ones(len(matrix)), matrix])
        scores = design @ self.coef_
        return scores if rows is None else scores[rows]

    def describe(self) -> dict[str, Any]:
        top: list[tuple[str, float]] = []
        if self.coef_ is not None and self.names:
            for name, weight in zip(self.names, self.coef_[1:], strict=False):
                top.append((name, float(weight)))
            top.sort(key=lambda kv: -abs(kv[1]))
        return {
            "level": self.level,
            "alpha": self.alpha,
            "features": len(self.names or []),
            "top_weights": [{"feature": name, "weight": round(w, 5)} for name, w in top[:8]],
        }


@dataclass
class _Tree:
    feature: int
    threshold: float
    left: float
    right: float


class BoostingModel:
    """Level 3: shallow squared-loss gradient boosting, written out in full.

    Depth-limited stumps/small trees, shrinkage, early stopping on a validation slice and
    a minimum leaf size. It is here to answer one question - "does a non-linear model beat
    ridge out of sample?" - not to be a production gradient booster.
    """

    level = "boosting"

    def __init__(
        self,
        *,
        depth: int = 2,
        n_estimators: int = 120,
        learning_rate: float = 0.05,
        min_leaf: int = 20,
        early_stopping_rounds: int = 15,
        names: Sequence[str] | None = None,
        seed: int = 7,
    ) -> None:
        self.depth = depth
        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.min_leaf = min_leaf
        self.early_stopping_rounds = early_stopping_rounds
        self.names = list(names) if names else None
        self.seed = seed
        self.trees_: list[_Tree] = []
        self.base_ = 0.0
        self.best_iteration_ = 0

    def _matrix(self, frame: FeatureFrame) -> Array:
        names = self.names or frame.names
        return np.nan_to_num(frame.matrix(names), nan=0.0)

    def fit(self, frame: FeatureFrame, train: Array, target: Array) -> BoostingModel:
        self.names = list(self.names or frame.names)
        matrix = self._matrix(frame)
        usable = ~np.isnan(target[train])
        rows = train[usable]
        if rows.size < 40:
            return self
        rng = np.random.default_rng(self.seed)
        rng.shuffle(rows)
        cut = int(rows.size * 0.8)
        fit_rows, val_rows = rows[:cut], rows[cut:]

        x, y = matrix[fit_rows], target[fit_rows]
        self.base_ = float(np.mean(y))
        prediction = np.full(len(fit_rows), self.base_)
        val_prediction = np.full(len(val_rows), self.base_)
        val_y = target[val_rows]

        best_loss = np.inf
        best_trees: list[_Tree] = []
        stale = 0
        for _ in range(self.n_estimators):
            residual = y - prediction
            tree = self._grow(x, residual)
            if tree is None:
                break
            self.trees_.append(tree)
            prediction += self.learning_rate * self._apply(tree, x)
            val_prediction += self.learning_rate * self._apply(tree, matrix[val_rows])
            loss = float(np.mean((val_y - val_prediction) ** 2))
            if loss < best_loss - 1e-12:
                best_loss = loss
                best_trees = list(self.trees_)
                stale = 0
            else:
                stale += 1
                if stale >= self.early_stopping_rounds:
                    break
        self.trees_ = best_trees
        self.best_iteration_ = len(best_trees)
        return self

    def _grow(self, x: Array, residual: Array) -> _Tree | None:
        best: tuple[float, int, float, float, float] | None = None
        n_rows, n_features = x.shape
        for feature in range(n_features):
            column = x[:, feature]
            # candidate thresholds at quantiles: cheap and scale-free
            quantiles = np.unique(np.quantile(column, np.linspace(0.1, 0.9, 9)))
            for threshold in quantiles:
                left = column <= threshold
                if left.sum() < self.min_leaf or (~left).sum() < self.min_leaf:
                    continue
                left_mean = float(np.mean(residual[left]))
                right_mean = float(np.mean(residual[~left]))
                gain = float(left.sum()) * left_mean**2 + float((~left).sum()) * right_mean**2
                if best is None or gain > best[0]:
                    best = (gain, feature, float(threshold), left_mean, right_mean)
        if best is None or best[0] <= 0:
            return None
        _, feature, threshold, left_mean, right_mean = best
        return _Tree(feature=feature, threshold=threshold, left=left_mean, right=right_mean)

    @staticmethod
    def _apply(tree: _Tree, x: Array) -> Array:
        return np.where(x[:, tree.feature] <= tree.threshold, tree.left, tree.right)

    def predict(self, frame: FeatureFrame, rows: Array | None = None) -> Array:
        matrix = self._matrix(frame)
        out = np.full(len(matrix), self.base_)
        for tree in self.trees_:
            out += self.learning_rate * self._apply(tree, matrix)
        return out if rows is None else out[rows]

    def describe(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "trees": len(self.trees_),
            "best_iteration": self.best_iteration_,
            "depth": self.depth,
            "learning_rate": self.learning_rate,
            "features": len(self.names or []),
        }


# ---------------------------------------------------------------------------
# probability calibration
# ---------------------------------------------------------------------------


def platt_scale(scores: Array, outcomes: Array) -> tuple[float, float]:
    """Fit ``P(up) = sigmoid(a * score + b)`` by a few Newton steps.

    Calibration is checked, not assumed: `brier` is reported next to it, because a model
    that ranks well but is badly calibrated produces position sizes that are wrong even
    when the direction is right.
    """
    mask = ~(np.isnan(scores) | np.isnan(outcomes))
    x = scores[mask]
    y = outcomes[mask]
    if x.size < 20:
        return 1.0, 0.0
    a, b = 1.0, 0.0
    for _ in range(100):
        z = a * x + b
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        w = np.clip(p * (1.0 - p), 1e-6, None)
        grad = np.array([np.sum((p - y) * x), np.sum(p - y)])
        hessian = np.array(
            [
                [np.sum(w * x * x) + 1e-6, np.sum(w * x)],
                [np.sum(w * x), np.sum(w) + 1e-6],
            ]
        )
        step = np.linalg.solve(hessian, grad)
        a -= float(step[0])
        b -= float(step[1])
        if np.all(np.abs(step) < 1e-8):
            break
    return float(a), float(b)


def brier(scores: Array, outcomes: Array, a: float, b: float) -> float:
    mask = ~(np.isnan(scores) | np.isnan(outcomes))
    if mask.sum() == 0:
        return 1.0
    p = 1.0 / (1.0 + np.exp(-np.clip(a * scores[mask] + b, -30, 30)))
    return float(np.mean((p - outcomes[mask]) ** 2))


# ---------------------------------------------------------------------------
# running the ladder
# ---------------------------------------------------------------------------


@dataclass
class LevelResult:
    level: str
    folds: int
    net_expectancy: float
    gross_expectancy: float
    hit_rate: float
    ic: float
    turnover: float
    trades: int
    brier: float = 1.0
    fold_expectancies: list[float] = field(default_factory=list)
    description: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "folds": self.folds,
            "net_expectancy_bps": round(self.net_expectancy * 10_000, 3),
            "gross_expectancy_bps": round(self.gross_expectancy * 10_000, 3),
            "hit_rate": round(self.hit_rate, 4),
            "ic": round(self.ic, 5),
            "turnover": round(self.turnover, 4),
            "trades": self.trades,
            "brier": round(self.brier, 4),
            "fold_expectancy_bps": [round(v * 10_000, 3) for v in self.fold_expectancies],
            "description": self.description,
        }


def _run_level(
    level: str,
    build: Callable[[], Any],
    frame: FeatureFrame,
    folds: Sequence[Fold],
    target: Array,
    *,
    fee_per_side: float,
) -> LevelResult:
    gross_parts: list[float] = []
    net_parts: list[float] = []
    hits: list[float] = []
    ics: list[float] = []
    turnovers: list[float] = []
    trades = 0
    fold_expectancies: list[float] = []
    description: dict[str, Any] = {}
    all_scores: list[float] = []
    all_outcomes: list[float] = []

    for fold in folds:
        model = build()
        model.fit(frame, fold.train, target)
        description = model.describe()
        scores = np.nan_to_num(model.predict(frame, fold.test))
        outcomes = target[fold.test]
        mask = ~np.isnan(outcomes)
        if mask.sum() == 0:
            continue
        scores, outcomes = scores[mask], outcomes[mask]
        positions = np.sign(scores)
        gross = float(np.mean(positions * outcomes))
        changes = float(np.sum(np.abs(np.diff(np.concatenate([[0.0], positions])))))
        net = gross - changes * fee_per_side / max(1, len(outcomes))
        fold_expectancies.append(net)
        gross_parts.append(gross * len(outcomes))
        net_parts.append(net * len(outcomes))
        hits.append(float(np.mean(np.sign(scores) == np.sign(outcomes))))
        ics.append(rank_correlation(scores, outcomes))
        turnovers.append(changes / max(1, len(outcomes)))
        trades += int(changes)
        all_scores.extend(scores.tolist())
        all_outcomes.extend(outcomes.tolist())

    total = sum(len(f.test) for f in folds) or 1
    mean_gross = sum(gross_parts) / total
    mean_net = sum(net_parts) / total
    a, b = platt_scale(np.array(all_scores), (np.array(all_outcomes) > 0).astype(float))
    return LevelResult(
        level=level,
        folds=len(folds),
        net_expectancy=mean_net,
        gross_expectancy=mean_gross,
        hit_rate=float(np.mean(hits)) if hits else 0.0,
        ic=float(np.mean(ics)) if ics else 0.0,
        turnover=float(np.mean(turnovers)) if turnovers else 0.0,
        trades=trades,
        brier=brier(np.array(all_scores), (np.array(all_outcomes) > 0).astype(float), a, b),
        fold_expectancies=fold_expectancies,
        description=description,
    )


@dataclass
class LadderResult:
    levels: list[LevelResult]
    promoted: str
    verdict: str
    windows: int
    promotion_rule: str = "a rung must beat the one below by 10% net expectancy in every window"

    def as_dict(self) -> dict[str, Any]:
        return {
            "levels": [level.as_dict() for level in self.levels],
            "promoted": self.promoted,
            "verdict": self.verdict,
            "windows": self.windows,
            "promotion_rule": self.promotion_rule,
        }


def promote(lower: LevelResult, higher: LevelResult, *, margin: float = 0.10) -> tuple[bool, str]:
    """The promotion test, applied rung by rung.

    The higher rung must (a) be profitable net of costs, (b) beat the lower rung by at
    least ``margin`` in aggregate, and (c) win in every walk-forward window. All three,
    because a single lucky window is how a more complex model gets adopted.
    """
    if higher.net_expectancy <= 0:
        return False, f"{higher.level} is not profitable net of costs ({higher.net_expectancy * 10_000:+.2f} bps)"
    if lower.net_expectancy <= 0 < higher.net_expectancy:
        return True, (
            f"{higher.level} is profitable where {lower.level} is not "
            f"({higher.net_expectancy * 10_000:+.2f} vs {lower.net_expectancy * 10_000:+.2f} bps)"
        )
    needed = lower.net_expectancy * (1.0 + margin)
    worse_windows = sum(
        1
        for low, high in zip(lower.fold_expectancies, higher.fold_expectancies, strict=False)
        if high < low
    )
    if higher.net_expectancy < needed:
        return False, (
            f"{higher.level} {higher.net_expectancy * 10_000:+.2f} bps does not clear "
            f"{lower.level} {lower.net_expectancy * 10_000:+.2f} bps by {margin:.0%}"
        )
    if worse_windows:
        return False, (
            f"{higher.level} loses in {worse_windows} of {len(higher.fold_expectancies)} windows - "
            "not a stable improvement"
        )
    return True, f"{higher.level} earns its complexity by {margin:.0%} in every window"


def run_ladder(
    frame: FeatureFrame,
    *,
    horizon: int = 4,
    n_folds: int = 4,
    fee_per_side: float = 0.001,
    specs: Sequence[SignalSpec] | None = None,
    ridge_alpha: float = 10.0,
    boosting_estimators: int = 120,
) -> LadderResult:
    """Walk the three rungs on the same purged folds and apply the promotion rule."""
    target = forward_returns(frame.close, horizon)
    usable = ~np.isnan(target)
    # keep the frame's row count; masks handle the edges
    folds = purged_folds(len(frame), n_folds=n_folds, horizon=horizon)
    if not folds:
        raise ValueError("not enough rows for a walk-forward ladder")

    chosen = list(specs or SIGNALS)
    baseline = _run_level(
        "baseline",
        lambda: BaselineModel(chosen),
        frame, folds, np.where(usable, target, np.nan), fee_per_side=fee_per_side,
    )
    ridge = _run_level(
        "ridge",
        lambda: RidgeModel(alpha=ridge_alpha),
        frame, folds, np.where(usable, target, np.nan), fee_per_side=fee_per_side,
    )
    boosting = _run_level(
        "boosting",
        lambda: BoostingModel(n_estimators=boosting_estimators),
        frame, folds, np.where(usable, target, np.nan), fee_per_side=fee_per_side,
    )

    levels = [baseline, ridge, boosting]
    promoted = baseline.level
    notes = []
    for lower, higher in zip(levels, levels[1:], strict=False):
        ok, reason = promote(lower, higher)
        notes.append(f"{higher.level}: {'PROMOTE' if ok else 'keep simpler'} - {reason}")
        if ok and promoted == lower.level:
            promoted = higher.level

    best = next(level for level in levels if level.level == promoted)
    if best.net_expectancy <= 0:
        verdict = (
            f"no rung is profitable net of {fee_per_side * 10_000:.0f} bps a side; "
            "the demonstration signal set has no edge on this data"
        )
    else:
        verdict = f"kept {promoted} at {best.net_expectancy * 10_000:+.2f} bps net per bar"
    return LadderResult(
        levels=levels,
        promoted=promoted,
        verdict=verdict + " | " + " ; ".join(notes),
        windows=len(folds),
    )


def fold_boundaries(n_samples: int, *, n_folds: int = 4, horizon: int = 4) -> Iterator[dict[str, int]]:
    """Human-readable fold geometry, for the report and for debugging leakage."""
    for fold in purged_folds(n_samples, n_folds=n_folds, horizon=horizon):
        yield {
            "fold": fold.index,
            "train_start": int(fold.train[0]),
            "train_end": int(fold.train[-1]),
            "test_start": int(fold.test[0]),
            "test_end": int(fold.test[-1]),
            "purged_rows": int(fold.test[0] - fold.train[-1] - 1),
            "embargo": horizon,
        }


def feature_importance(frame: FeatureFrame, *, horizon: int = 4, n_folds: int = 3) -> list[dict[str, Any]]:
    """Ridge weight magnitude as a crude importance, with the leakage smells flagged.

    A single feature carrying most of the weight is a warning, not a triumph: it is what
    an index-like or time-like leak looks like.
    """
    target = forward_returns(frame.close, horizon)
    folds = purged_folds(len(frame), n_folds=n_folds, horizon=horizon)
    matrix = frame.matrix()
    usable_rows = ~np.isnan(matrix).any(axis=1) & ~np.isnan(target)
    if folds and usable_rows.any():
        fold = folds[0]
        model = RidgeModel(alpha=10.0, names=frame.names).fit(frame, fold.train, target)
    else:  # pragma: no cover - tiny frames
        model = RidgeModel(alpha=10.0, names=frame.names)
    weights: list[dict[str, Any]] = []
    if model.coef_ is not None:
        for name, weight in zip(model.names or [], model.coef_[1:], strict=False):
            weights.append({"feature": name, "weight": round(float(weight), 5)})
    weights.sort(key=lambda row: -abs(row["weight"]))
    total = sum(abs(row["weight"]) for row in weights) or 1.0
    for row in weights:
        row["share"] = round(abs(row["weight"]) / total, 4)
    if weights and weights[0]["share"] > 0.4:
        weights[0]["warning"] = "one feature carries over 40% of the weight - check for leakage"
    return weights


def shuffled_label_test(
    frame: FeatureFrame,
    *,
    horizon: int = 4,
    n_folds: int = 3,
    seed: int = 11,
) -> dict[str, Any]:
    """Shuffle the labels: predictive power must vanish. If it survives, we are leaking.

    Returns the in-sample and shuffled IC of a ridge fit on the first fold.
    """
    target = forward_returns(frame.close, horizon)
    folds = purged_folds(len(frame), n_folds=n_folds, horizon=horizon)
    if not folds:
        return {"available": False}
    fold = folds[0]
    rng = np.random.default_rng(seed)
    real = RidgeModel(alpha=10.0, names=frame.names).fit(frame, fold.train, target)
    real_ic = rank_correlation(np.nan_to_num(real.predict(frame, fold.test)), target[fold.test])

    finite = np.flatnonzero(~np.isnan(target))
    shuffled_ics: list[float] = []
    # One permutation is noise: on a 200-row test block |IC| of 0.1 happens by chance.
    # Several permutations, compared with the sampling band for this sample size, is a
    # test. `1.96 / sqrt(n)` is the 95% band for a correlation that should be zero.
    for _ in range(9):
        shuffled_target = target.copy()
        shuffled_target[finite] = rng.permutation(target[finite])
        fake = RidgeModel(alpha=10.0, names=frame.names).fit(frame, fold.train, shuffled_target)
        shuffled_ics.append(
            rank_correlation(np.nan_to_num(fake.predict(frame, fold.test)), target[fold.test])
        )
    shuffled_ic = float(np.mean(shuffled_ics))
    n_test = int(np.sum(~np.isnan(target[fold.test]))) or 1
    noise_band = 1.96 / np.sqrt(n_test)
    # inside the noise band, or no larger than a third of the real IC (a leak would leave
    # the shuffled score predicting about as well as the real one)
    collapsed = abs(shuffled_ic) <= max(noise_band, abs(real_ic) / 3.0)
    return {
        "available": True,
        "real_ic": round(real_ic, 5),
        "shuffled_ic": round(shuffled_ic, 5),
        "noise_band": round(float(noise_band), 4),
        "shuffled_ics": [round(v, 4) for v in shuffled_ics],
        "collapsed": bool(collapsed),
        "verdict": (
            f"labels shuffled {len(shuffled_ics)}x -> mean IC {shuffled_ic:+.3f} is inside the "
            f"+/-{noise_band:.3f} noise band, so the pipeline is not leaking"
            if collapsed
            else (
                f"shuffled labels still predict (mean IC {shuffled_ic:+.3f} against real "
                f"{real_ic:+.3f}, band +/-{noise_band:.3f}) - investigate before trusting anything"
            )
        ),
    }


def time_shift_test(
    frame: FeatureFrame, *, horizon: int = 4, shift: int = 50, far_shift: int | None = None
) -> dict[str, Any]:
    """Shift the scores forward in time: real signal decays, a leak does not.

    Two shifts are checked. A *small* shift may retain IC simply because markets trend for
    a while, so the verdict uses a *far* shift (default: half the sample). If a score still
    predicts returns hundreds of bars out of alignment, the feature or the target is
    carrying information it should not have.
    """
    from .signals import SIGNALS as _SIGNALS

    if not _SIGNALS:
        return {"available": False}
    spec = _SIGNALS[0]
    scores = spec.compute(frame)
    target = forward_returns(frame.close, horizon)

    def shifted(by: int) -> Array:
        out = np.full(len(scores), np.nan)
        if 0 < by < len(scores):
            out[by:] = scores[:-by]
        return out

    far = far_shift if far_shift is not None else max(shift, len(scores) // 2)
    ic_now = rank_correlation(scores, target)
    ic_near = rank_correlation(shifted(shift), target)
    ic_far = rank_correlation(shifted(far), target)
    band = 1.96 / np.sqrt(max(30, int(np.sum(~np.isnan(target)))))
    decays = abs(ic_far) <= band
    return {
        "available": True,
        "signal": spec.name,
        "ic_now": round(ic_now, 5),
        "ic_near_shift": round(ic_near, 5),
        "ic_far_shift": round(ic_far, 5),
        "far_shift_bars": far,
        "noise_band": round(float(band), 4),
        "decays": bool(decays),
    }


def correlation_to_target(frame: FeatureFrame, horizon: int = 4) -> list[dict[str, Any]]:
    """Feature-level IC, so a reviewer can see which inputs carry any information at all."""
    target = forward_returns(frame.close, horizon)
    rows = []
    for name in frame.names:
        rows.append(
            {
                "feature": name,
                "family": frame.families.get(name, ""),
                "ic": round(rank_correlation(frame.features[name], target), 5),
                "pearson": round(pearson(frame.features[name], target), 5),
            }
        )
    return sorted(rows, key=lambda row: -abs(row["ic"]))


def evaluate_ladder_ranking(results: Sequence[LevelResult]) -> list[dict[str, Any]]:
    """Side-by-side table for the report."""
    return [
        {
            "level": result.level,
            "net_bps": round(result.net_expectancy * 10_000, 3),
            "gross_bps": round(result.gross_expectancy * 10_000, 3),
            "ic": round(result.ic, 4),
            "hit": round(result.hit_rate, 3),
            "trades": result.trades,
        }
        for result in results
    ]
