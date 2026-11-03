"""S4: the model-driven signal, wired to a registry and inert without an approved card.

The placeholder this replaces was honest but useless: it returned nothing forever. This
version returns nothing *until* an approved model card exists, and then it works only
within the guardrails §19.8 demands:

* it reads a card from disk; it never trains, and it never writes;
* if the card's feature set does not match what the feature store produces, it returns no
  signal rather than a signal computed from the wrong inputs;
* if a required feature is mostly NaN (stale feed, missing book), it returns no signal -
  the fail-safe is "no trade", never "trade on zeros";
* the score is only a *candidate*: the risk engine still sizes it and can veto it.

There is no model card in this repository. Training one needs real recorded data and the
same gates as the rule-based strategies; shipping a made-up card would be exactly the
dishonesty this project is trying not to commit.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..ai.features import FeatureStore
from ..ai.registry import PromotedModel, load_model
from ..core.models import OrderType, Signal
from .base import Strategy, StrategyContext, StrategyParams


class MlSignal(Strategy):
    name = "S4_ml_signal"
    min_bars = 200
    #: score magnitude below which the model is not confident enough to trade
    entry_threshold = 0.35

    def __init__(self, params: StrategyParams) -> None:
        super().__init__(params)
        registry = self.p.get("registry")
        self.registry_path = Path(str(registry)) if registry else Path("models/registry.json")
        self.model: PromotedModel | None = load_model(self.registry_path)
        self.store = FeatureStore()
        self.last_check: dict[str, Any] = {}
        self.refusals = 0
        self.entry_threshold = self.p.f("entry_threshold", self.entry_threshold)

    @property
    def status(self) -> str:
        if self.model is None:
            return f"not_promoted: no model card at {self.registry_path}"
        if not self.model.ready:
            return "not_promoted: model card is not approved or has no weights"
        return f"ready: {self.model.card.name} ({self.model.card.level})"

    # --- evaluation ------------------------------------------------------
    def evaluate(self, ctx: StrategyContext) -> Signal | None:
        if self.model is None or not self.model.ready:
            return None  # the normal state of this repository

        candles = ctx.series.last(self.min_bars)
        if len(candles) < self.min_bars:
            return None
        books: dict[datetime, Any] | None = {ctx.ts: ctx.book} if ctx.book is not None else None
        try:
            frame = self.store.compute(candles, books=books)
        except (ValueError, KeyError):
            self.refusals += 1
            return None

        self.last_check = self.model.check(frame)
        if not self.last_check.get("ready"):
            self.refusals += 1
            return None

        score = float(self.model.score(frame)[-1])
        threshold = float(self.model.card.threshold or 0.0)
        adjusted = score - threshold
        if adjusted < self.entry_threshold:
            # long-only book: a weak or negative score is no trade, not a short
            return None

        stop_distance = ctx.atr * self.p.f("stop_atr_mult", 1.75) if ctx.atr > 0 else 0.0
        if stop_distance <= 0:
            self.refusals += 1
            return None
        return self.build_signal(
            ctx,
            stop_distance=stop_distance,
            entry=ctx.mark,
            confidence=min(0.9, 0.4 + adjusted),
            reason=(
                f"model {self.model.card.name} v{self.model.card.version} "
                f"score {score:+.3f} >= {self.entry_threshold:g}"
            ),
            entry_type=OrderType.LIMIT,
            features={"model_score": f"{score:.4f}"},
        )

    def describe(self) -> dict[str, Any]:
        return {
            "strategy": self.name,
            "status": self.status,
            "registry": str(self.registry_path),
            "refusals": self.refusals,
            "last_check": self.last_check,
        }
