"""S4: ML signal placeholder.

Deliberately inert. Section 8 of the build plan says a strategy ships only after it
clears the acceptance gates, and section 19 says a model must beat a simpler
baseline out-of-sample before it is promoted. Until a model is trained, validated
and registered, this class must not produce signals - returning None here is the
honest behaviour, not a bug.
"""

from __future__ import annotations

from ..core.models import Signal
from .base import Strategy, StrategyContext


class MlSignal(Strategy):
    name = "S4_ml_signal"
    min_bars = 200

    def evaluate(self, ctx: StrategyContext) -> Signal | None:  # noqa: ARG002
        return None

    @property
    def status(self) -> str:
        return "not_promoted"
