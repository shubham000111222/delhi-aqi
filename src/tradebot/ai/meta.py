"""Meta-labelling (§19.3): a second model that decides whether to *take* a rule's trade.

The rule-based strategies are good at finding candidates and bad at knowing which ones
will work. Meta-labelling keeps them as the candidate generator and learns a second
question: "given this setup, does the target get hit before the stop?"

The label comes from the triple-barrier method, and it is the only place in this package
that looks forward in time:

* **upper barrier** - target, ``target_atr`` x ATR above entry;
* **lower barrier** - stop, ``stop_atr`` x ATR below entry;
* **vertical barrier** - ``max_bars`` bars, after which the trade is closed at the market
  and labelled by whether it is above water.

Three things make the result trustworthy, and each of them is a place this kind of model
usually goes wrong:

1. **Overlapping labels are purged.** A trade opened at bar *t* resolves somewhere in
   *[t+1, t+max_bars]*; two candidates inside that window share an outcome. Folds drop
   any training row whose label window touches the test window.
2. **The threshold is chosen on validation data to maximise post-cost expectancy**, not
   accuracy. A model that is 90% accurate on trades that make nothing is useless.
3. **The gate can only remove trades.** A positive prediction never invents a trade and
   never enlarges one - position size stays with the risk engine. That is the §19.8
   guardrail, and it is enforced by the return type: `MetaLabelDecision.take` is a bool.

There is no trained meta-model in this repository, for the same reason there is no S4
card: the synthetic feed has no edge to learn. The code exists so that when real data
arrives, the gate is ready and honest.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..strategies.indicators import atr
from .features import Array, FeatureFrame
from .ladder import purged_folds

EPS = 1e-12


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


@dataclass
class LabelledSetup:
    index: int
    label: int          # 1 = target hit first, 0 = stop (or time) hit first
    outcome: float      # realised return to the barrier touch, in return units
    held_bars: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "outcome": round(self.outcome, 6),
            "held_bars": self.held_bars,
        }


def triple_barrier(
    close: Array,
    high: Array,
    low: Array,
    *,
    candidates: Array,
    target_atr: float = 1.5,
    stop_atr: float = 1.0,
    max_bars: int = 60,
    atr_period: int = 14,
) -> list[LabelledSetup]:
    """Label every candidate long entry by which barrier it touches first.

    Ties (high and low both breached in the same bar) are labelled as the *stop*: when a
    bar is ambiguous, the pessimistic reading is the honest one.
    """
    volatility = atr(high, low, close, atr_period)
    setups: list[LabelledSetup] = []
    n = len(close)
    for raw in np.flatnonzero(candidates):
        index = int(raw)
        if index >= n - 1 or np.isnan(volatility[index]) or volatility[index] <= 0:
            continue
        entry = float(close[index])
        upper = entry + target_atr * float(volatility[index])
        lower = entry - stop_atr * float(volatility[index])
        resolved = False
        for step in range(1, max_bars + 1):
            j = index + step
            if j >= n:
                break
            if low[j] <= lower:
                setups.append(LabelledSetup(index, 0, lower / entry - 1.0, int(step)))
                resolved = True
                break
            if high[j] >= upper:
                setups.append(LabelledSetup(index, 1, upper / entry - 1.0, int(step)))
                resolved = True
                break
        if not resolved:
            j = min(index + max_bars, n - 1)
            j = int(j)
            outcome = float(close[j]) / entry - 1.0
            # the vertical barrier: a small profit counts as a win, but only just
            setups.append(LabelledSetup(index, 1 if outcome > 0 else 0, outcome, int(j - index)))
    return setups


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------


class LogisticGate:
    """L2-regularised logistic regression, fitted by Newton steps. No dependencies."""

    def __init__(self, alpha: float = 1.0, class_balance: bool = True) -> None:
        self.alpha = alpha
        self.class_balance = class_balance
        self.coef_: Array | None = None
        self.mean_: Array | None = None
        self.scale_: Array | None = None

    def fit(self, x: Array, y: Array) -> LogisticGate:
        if len(np.unique(y)) < 2 or len(y) < 20:
            return self  # too little evidence to fit anything; `ready` stays False
        self.mean_ = x.mean(axis=0)
        self.scale_ = x.std(axis=0)
        self.scale_[self.scale_ <= EPS] = 1.0
        z = (x - self.mean_) / self.scale_
        design = np.column_stack([np.ones(len(z)), z])
        weights = np.zeros(design.shape[1])
        # class weights: a 90/10 label split would otherwise learn "always no"
        if self.class_balance:
            positives = max(1, int(y.sum()))
            negatives = max(1, len(y) - positives)
            sample_weight = np.where(y > 0, len(y) / (2.0 * positives), len(y) / (2.0 * negatives))
        else:
            sample_weight = np.ones(len(y))
        penalty = np.eye(design.shape[1]) * self.alpha
        penalty[0, 0] = 0.0
        for _ in range(25):
            z_lin = design @ weights
            p = 1.0 / (1.0 + np.exp(-np.clip(z_lin, -30, 30)))
            gradient = design.T @ (sample_weight * (p - y)) + penalty @ weights
            w = sample_weight * np.clip(p * (1.0 - p), 1e-6, None)
            hessian = design.T @ (design * w[:, None]) + penalty
            try:
                step = np.linalg.solve(hessian, gradient)
            except np.linalg.LinAlgError:  # pragma: no cover - singular in tiny samples
                break
            weights -= step
            if np.max(np.abs(step)) < 1e-8:
                break
        self.coef_ = weights
        return self

    @property
    def ready(self) -> bool:
        return self.coef_ is not None

    def predict_proba(self, x: Array) -> Array:
        if not self.ready or self.coef_ is None or self.mean_ is None or self.scale_ is None:
            return np.full(len(x), np.nan)
        z = (x - self.mean_) / self.scale_
        design = np.column_stack([np.ones(len(z)), z])
        linear = design @ self.coef_
        return 1.0 / (1.0 + np.exp(-np.clip(linear, -30, 30)))

    def describe(self) -> dict[str, Any]:
        return {
            "fitted": self.ready,
            "alpha": self.alpha,
            "class_balance": self.class_balance,
            "coefficients": [round(float(c), 5) for c in (self.coef_ or [])],
        }


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


@dataclass
class MetaLabelDecision:
    take: bool
    probability: float
    threshold: float
    reason: str
    expected_r: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "take": self.take,
            "probability": round(self.probability, 4),
            "threshold": round(self.threshold, 4),
            "reason": self.reason,
            "expected_r": round(self.expected_r, 4),
        }


@dataclass
class MetaReport:
    setups: int
    base_rate: float
    folds: int
    auc: float
    precision_at_threshold: float
    recall_at_threshold: float
    trades_taken: int
    trades_skipped: int
    expectancy_all: float
    expectancy_taken: float
    expectancy_skipped: float
    threshold: float
    feature_weights: list[dict[str, Any]] = field(default_factory=list)
    verdict: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "setups": self.setups,
            "base_rate": round(self.base_rate, 4),
            "folds": self.folds,
            "auc": round(self.auc, 4),
            "precision_at_threshold": round(self.precision_at_threshold, 4),
            "recall_at_threshold": round(self.recall_at_threshold, 4),
            "trades_taken": self.trades_taken,
            "trades_skipped": self.trades_skipped,
            "expectancy_all": round(self.expectancy_all, 6),
            "expectancy_taken": round(self.expectancy_taken, 6),
            "expectancy_skipped": round(self.expectancy_skipped, 6),
            "threshold": round(self.threshold, 4),
            "feature_weights": self.feature_weights,
            "verdict": self.verdict,
        }


def auc_score(probabilities: Array, labels: Array) -> float:
    """Rank-based AUC (ties handled), 0.5 = coin flip."""
    mask = ~(np.isnan(probabilities) | np.isnan(labels))
    p, y = probabilities[mask], labels[mask]
    positives = int(y.sum())
    negatives = len(y) - positives
    if positives == 0 or negatives == 0:
        return 0.5
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p), dtype=float)
    ranks[order] = np.arange(1, len(p) + 1)
    _, inverse, counts = np.unique(p, return_inverse=True, return_counts=True)
    if (counts > 1).any():  # average ranks across ties
        sums = np.zeros(len(counts))
        np.add.at(sums, inverse, ranks)
        ranks = (sums / counts)[inverse]
    return float((ranks[y > 0].sum() - positives * (positives + 1) / 2.0) / (positives * negatives))


def choose_threshold(
    probabilities: Array,
    outcomes: Array,
    *,
    cost_per_trade: float,
    grid: Sequence[float] | None = None,
) -> float:
    """Pick the probability cut-off with the best *post-cost* expectancy.

    Not accuracy. A cut-off that maximises accuracy usually accepts many small winners and
    declines the few large ones, which is the opposite of what a trading account wants.
    """
    thresholds = list(grid) if grid is not None else [i / 20 for i in range(1, 20)]
    best_threshold, best_value = 1.1, -math.inf
    for threshold in thresholds:
        chosen = probabilities >= threshold
        if chosen.sum() < 5:
            continue
        value = float(np.mean(outcomes[chosen]) - cost_per_trade)
        if value > best_value:
            best_threshold, best_value = threshold, value
    return best_threshold if best_threshold <= 1.0 else 0.5


def evaluate_meta(
    frame: FeatureFrame,
    *,
    candidates: Array,
    target_atr: float = 1.5,
    stop_atr: float = 1.0,
    max_bars: int = 60,
    n_folds: int = 4,
    alpha: float = 1.0,
    cost_per_trade: float = 0.002,
    features: Sequence[str] | None = None,
) -> tuple[MetaReport, LogisticGate, float]:
    """Walk the meta-model through purged folds and report what the gate is worth.

    Returns the report, a model fitted on all labelled setups (for card writing) and the
    chosen threshold.
    """
    highs = _series_from(frame, "high")
    lows = _series_from(frame, "low")
    setups = triple_barrier(
        frame.close, highs, lows,
        candidates=candidates, target_atr=target_atr, stop_atr=stop_atr,
        max_bars=max_bars,
    )
    if len(setups) < 40:
        empty = MetaReport(
            setups=len(setups), base_rate=0.0, folds=0, auc=0.5,
            precision_at_threshold=0.0, recall_at_threshold=0.0,
            trades_taken=0, trades_skipped=len(setups),
            expectancy_all=0.0, expectancy_taken=0.0, expectancy_skipped=0.0,
            threshold=0.5, verdict="too few candidate setups to fit a meta-model",
        )
        return empty, LogisticGate(alpha=alpha), 0.5

    names = list(features or frame.names)
    rows = np.array([s.index for s in setups])
    labels = np.array([s.label for s in setups], dtype=float)
    outcomes = np.array([s.outcome for s in setups], dtype=float)

    matrix = frame.matrix(names)
    usable = ~np.isnan(matrix[rows]).any(axis=1)
    rows, labels, outcomes = rows[usable], labels[usable], outcomes[usable]
    if len(rows) < 40:
        empty = MetaReport(
            setups=len(rows), base_rate=0.0, folds=0, auc=0.5,
            precision_at_threshold=0.0, recall_at_threshold=0.0,
            trades_taken=0, trades_skipped=len(rows),
            expectancy_all=0.0, expectancy_taken=0.0, expectancy_skipped=0.0,
            threshold=0.5, verdict="candidates exist but their features are mostly NaN",
        )
        return empty, LogisticGate(alpha=alpha), 0.5

    x = np.nan_to_num(matrix[rows])

    # Folds are cut on bar time, then every setup whose *label window* overlaps the test
    # block is purged. Two candidates 30 bars apart share most of their outcome.
    folds = purged_folds(len(frame), n_folds=n_folds, horizon=max_bars)
    probabilities = np.full(len(rows), np.nan)
    for fold in folds:
        test_start, test_end = int(fold.test[0]), int(fold.test[-1])
        test_mask = (rows >= test_start) & (rows <= test_end)
        train_mask = (rows < test_start - max_bars) & (rows < test_start)
        if test_mask.sum() < 5 or train_mask.sum() < 20:
            continue
        model = LogisticGate(alpha=alpha).fit(x[train_mask], labels[train_mask])
        if not model.ready:
            continue
        probabilities[test_mask] = model.predict_proba(x[test_mask])

    evaluated = ~np.isnan(probabilities)
    if evaluated.sum() < 20:
        empty = MetaReport(
            setups=len(rows), base_rate=float(labels.mean()), folds=0, auc=0.5,
            precision_at_threshold=0.0, recall_at_threshold=0.0,
            trades_taken=0, trades_skipped=len(rows),
            expectancy_all=float(outcomes.mean()), expectancy_taken=0.0,
            expectancy_skipped=0.0, threshold=0.5,
            verdict="not enough out-of-sample predictions to judge the gate",
        )
        return empty, LogisticGate(alpha=alpha), 0.5

    threshold = choose_threshold(
        probabilities[evaluated], outcomes[evaluated], cost_per_trade=cost_per_trade
    )
    taken = evaluated & (probabilities >= threshold)
    skipped = evaluated & (probabilities < threshold)
    precision = float(labels[taken].mean()) if taken.sum() else 0.0
    recall = float(taken.sum() / max(1, int(labels[evaluated].sum())))

    final_model = LogisticGate(alpha=alpha).fit(x, labels)
    weights: list[dict[str, Any]] = []
    if final_model.coef_ is not None:
        for name, weight in zip(names, final_model.coef_[1:], strict=False):
            weights.append({"feature": name, "weight": round(float(weight), 5)})
        weights.sort(key=lambda row: -abs(row["weight"]))

    expectancy_all = float(outcomes[evaluated].mean())
    expectancy_taken = float(outcomes[taken].mean()) if taken.sum() else 0.0
    expectancy_skipped = float(outcomes[skipped].mean()) if skipped.sum() else 0.0
    improvement = expectancy_taken - expectancy_all
    if taken.sum() < 5:
        verdict = "no threshold produced enough trades to trust - the gate would never fire"
    elif improvement <= cost_per_trade:
        verdict = (
            f"gate adds {improvement * 10_000:+.1f} bps per trade, which does not cover "
            f"{cost_per_trade * 10_000:.0f} bps of costs - do not use it"
        )
    else:
        verdict = (
            f"gate improves expectancy by {improvement * 10_000:+.1f} bps per trade on "
            f"{int(taken.sum())} of {int(evaluated.sum())} setups"
        )

    return (
        MetaReport(
            setups=len(rows),
            base_rate=float(labels.mean()),
            folds=len(folds),
            auc=auc_score(probabilities[evaluated], labels[evaluated]),
            precision_at_threshold=precision,
            recall_at_threshold=recall,
            trades_taken=int(taken.sum()),
            trades_skipped=int(skipped.sum()),
            expectancy_all=expectancy_all,
            expectancy_taken=expectancy_taken,
            expectancy_skipped=expectancy_skipped,
            threshold=threshold,
            feature_weights=weights[:10],
            verdict=verdict,
        ),
        final_model,
        threshold,
    )


def _series_from(frame: FeatureFrame, name: str) -> Array:
    """High/low are not features; the frame keeps them only if the caller added them."""
    values = frame.features.get(f"_{name}")
    if values is None:
        # fall back to close: barriers become symmetric around the close, which biases
        # nothing about direction but does understate how often the stop is touched
        return frame.close
    return values


# ---------------------------------------------------------------------------
# the runtime gate
# ---------------------------------------------------------------------------


@dataclass
class MetaGate:
    """A loaded meta-model, applied to a live candidate before it becomes an order."""

    model: LogisticGate
    threshold: float
    features: list[str]
    name: str = "meta_label"
    version: str = "1"

    @property
    def ready(self) -> bool:
        return self.model.ready and bool(self.features)

    def decide(self, frame: FeatureFrame) -> MetaLabelDecision:
        if not self.ready:
            return MetaLabelDecision(False, float("nan"), self.threshold, "no model loaded")
        missing = [name for name in self.features if name not in frame.features]
        if missing:
            return MetaLabelDecision(
                False, float("nan"), self.threshold, f"missing features: {', '.join(missing)}"
            )
        row = np.nan_to_num(frame.matrix(self.features)[-1:], nan=0.0)
        probability = float(self.model.predict_proba(row)[0])
        if np.isnan(probability):
            return MetaLabelDecision(False, float("nan"), self.threshold, "model returned NaN")
        take = probability >= self.threshold
        return MetaLabelDecision(
            take,
            probability,
            self.threshold,
            f"P(target before stop) = {probability:.3f} vs threshold {self.threshold:.2f}",
            expected_r=float(probability * 1.5 - (1 - probability) * 1.0),
        )


def save_meta(
    path: str,
    *,
    name: str,
    model: LogisticGate,
    features: Sequence[str],
    threshold: float,
    report: MetaReport,
) -> dict[str, Any]:
    """Write a meta-model card. Refused unless the gate paid for its own costs."""
    import json
    from pathlib import Path

    if not model.ready:
        raise ValueError("cannot save an unfitted meta-model")
    if report.trades_taken < 5 or report.expectancy_taken <= 0:
        raise ValueError(
            "refusing to save a meta-model whose gated trades do not make money: "
            + report.verdict
        )
    payload = {
        "name": name,
        "kind": "meta_label",
        "features": list(features),
        "coefficients": [float(c) for c in (model.coef_ or [])],
        "mean": [float(v) for v in (model.mean_ or [])],
        "scale": [float(v) for v in (model.scale_ or [])],
        "threshold": float(threshold),
        "metrics": report.as_dict(),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2))
    return payload
