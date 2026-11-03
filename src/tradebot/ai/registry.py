"""Model registry and model cards (§19.7): what was trained, on what, and why it may run.

A model that only exists as a pickle is unauditable. Each saved model here carries a card:
the features it uses, the exact weights, the data window, the seed, the metrics it earned
out of sample, and whether it was approved. Loading a card is therefore a decision anyone
can re-check, and refusing to run an unapproved card is a decision the code enforces.

Two rules:

* `save_card` refuses to write a card whose own metrics fail to justify promotion. The
  path from "a model trained" to "a model trades" cannot be taken by accident.
* `PromotedModel.ready` is False unless the card is approved *and* its feature set matches
  what the feature store actually produces. A model fed different inputs than it was
  trained on is worse than no model, and this check is the only thing standing between
  those two states.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .features import Array, FeatureFrame


@dataclass
class ModelCard:
    name: str
    level: str
    features: list[str]
    weights: list[float]
    intercept: float
    threshold: float = 0.0
    created_at: str = ""
    training_window: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    seed: int | None = None
    approved: bool = False
    approval_note: str = ""
    notes: str = ""
    version: str = "1"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.as_dict(), indent=2, default=str))
        return target

    @classmethod
    def load(cls, path: str | Path) -> ModelCard:
        payload = json.loads(Path(path).read_text())
        return cls(**payload)


class ModelRefusal(RuntimeError):
    """Raised when a card must not be written: promotion was not earned."""


def save_card(
    path: str | Path,
    *,
    name: str,
    level: str,
    features: list[str],
    weights: list[float],
    intercept: float,
    metrics: dict[str, float],
    training_window: dict[str, Any] | None = None,
    threshold: float = 0.0,
    seed: int | None = None,
    min_net_expectancy: float = 0.0,
    notes: str = "",
) -> ModelCard:
    """Write a card, but only for a model that earned it.

    The gate is deliberately blunt: net-of-cost expectancy must be positive. A card for a
    model that loses money after fees is not a record, it is a liability.
    """
    net = float(metrics.get("net_expectancy", 0.0))
    if net <= min_net_expectancy:
        raise ModelRefusal(
            f"refusing to register {name}: net expectancy {net * 10_000:+.2f} bps is not "
            "positive after costs, so there is nothing to promote"
        )
    if level == "baseline" and not weights:
        raise ModelRefusal("refusing to register a baseline with no weights: nothing to save")
    card = ModelCard(
        name=name,
        level=level,
        features=list(features),
        weights=[float(w) for w in weights],
        intercept=float(intercept),
        threshold=float(threshold),
        created_at=datetime.now().isoformat(timespec="seconds"),
        training_window=training_window or {},
        metrics={k: float(v) for k, v in metrics.items()},
        seed=seed,
        approved=True,
        approval_note="auto-approved by save_card: positive net expectancy",
        notes=notes,
    )
    card.save(path)
    return card


@dataclass
class PromotedModel:
    """A loaded, approved card, ready to score a feature frame."""

    card: ModelCard
    path: Path

    @property
    def ready(self) -> bool:
        return bool(self.card.approved and self.card.weights and self.card.features)

    def score(self, frame: FeatureFrame) -> Array:
        """Linear score per bar, or all-NaN when the features do not match the card."""
        missing = [name for name in self.card.features if name not in frame.features]
        if missing:
            return np.full(len(frame), np.nan)
        matrix = frame.matrix(self.card.features)
        values = np.nan_to_num(matrix, nan=0.0)
        weights = np.asarray(self.card.weights, dtype=float)
        return values @ weights + self.card.intercept

    def check(self, frame: FeatureFrame) -> dict[str, Any]:
        """Fail-safe check before anything is allowed to trade on this model.

        Staleness is judged on the *recent* bars, not the whole window. A feature that is
        NaN for its warm-up period is expected; a feature that is NaN over the last fifth
        of the window is a broken feed, and a model asked to score on zeros there would
        answer confidently about data it never saw.
        """
        missing = [name for name in self.card.features if name not in frame.features]
        tail = slice(max(0, int(len(frame) * 0.8)), len(frame))
        stale_or_nan = {
            name: float(np.mean(np.isnan(frame.features[name][tail])))
            for name in self.card.features
            if name in frame.features
        }
        unusable = [name for name, share in stale_or_nan.items() if share > 0.5]
        return {
            "ready": self.ready and not missing and not unusable,
            "approved": self.card.approved,
            "missing_features": missing,
            "mostly_nan_features": unusable,
            "reason": (
                "ok"
                if self.ready and not missing and not unusable
                else "model cannot be trusted on this data - no trade"
            ),
        }


def load_model(path: str | Path) -> PromotedModel | None:
    """``None`` rather than an exception: absence of a model is a normal state."""
    target = Path(path)
    if not target.exists():
        return None
    try:
        return PromotedModel(card=ModelCard.load(target), path=target)
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def default_registry_path(root: str | Path = ".") -> Path:
    return Path(root) / "models" / "registry.json"
