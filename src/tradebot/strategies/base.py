"""Strategy interface.

A strategy is a pure function of market data plus its own parameters. It never
touches balances, the exchange or the ledger, which is what makes it replayable and
safe to run identically in backtest, paper and live.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..core.models import (
    OrderBook,
    OrderType,
    Position,
    Regime,
    Side,
    Signal,
)
from ..core.money import D
from ..data.series import CandleSeries


@dataclass(slots=True)
class StrategyContext:
    """Everything a strategy is allowed to see at bar close."""

    symbol: str
    timeframe: str
    series: CandleSeries
    ts: datetime
    mark: Decimal
    atr: float
    regime: Regime
    equity: Decimal
    book: OrderBook | None = None
    position: Position | None = None
    extra: dict[str, Any] | None = None


@dataclass(slots=True)
class StrategyParams:
    name: str
    timeframes: tuple[str, ...]
    regimes: tuple[str, ...]
    priority: int
    raw: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def f(self, key: str, default: float) -> float:
        value = self.raw.get(key, default)
        return default if value is None else float(value)

    def i(self, key: str, default: int) -> int:
        value = self.raw.get(key, default)
        return default if value is None else int(value)


class Strategy(ABC):
    """Base class. Subclasses implement :meth:`evaluate`."""

    #: human readable name, matches the key in strategies.yaml
    name: str = "strategy"
    #: minimum closed bars required before the strategy may emit a signal
    min_bars: int = 60
    #: target distance in R (multiples of the initial risk per unit)
    target_r: float = 2.0

    def __init__(self, params: StrategyParams) -> None:
        self.p = params

    # --- lifecycle --------------------------------------------------------
    @property
    def warmup(self) -> int:
        return self.min_bars

    def allows_regime(self, regime: Regime) -> bool:
        if not self.p.regimes:
            return True
        return regime.value in self.p.regimes

    def allows_timeframe(self, timeframe: str) -> bool:
        return not self.p.timeframes or timeframe in self.p.timeframes

    # --- signal construction ---------------------------------------------
    def build_signal(
        self,
        ctx: StrategyContext,
        *,
        stop_distance: float,
        entry: Decimal | None = None,
        target: Decimal | None = None,
        confidence: float = 0.5,
        reason: str = "",
        entry_type: OrderType = OrderType.LIMIT,
        features: dict[str, str] | None = None,
    ) -> Signal:
        """Long-only signal with an ATR-derived stop and a target in R."""
        if stop_distance <= 0:
            raise ValueError("stop_distance must be positive")
        entry_price = entry if entry is not None else ctx.mark
        stop = entry_price - D(stop_distance)
        if target is None:
            target = entry_price + D(stop_distance * self.target_r)
        if stop >= entry_price:
            raise ValueError("stop must be below entry for a long signal")
        return Signal(
            id=Signal.new_id(self.name, ctx.symbol, ctx.ts),
            strategy=self.name,
            symbol=ctx.symbol,
            timeframe=ctx.timeframe,
            side=Side.BUY,
            entry_type=entry_type,
            ts=ctx.ts,
            stop=stop,
            target=target,
            confidence=D(round(min(max(confidence, 0.0), 1.0), 4)),
            limit_price=entry_price if entry_type is OrderType.LIMIT else None,
            reason=reason,
            regime=ctx.regime,
            features=features or {},
        )

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> Signal | None:
        """Return at most one signal for this bar, or None."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.name} tf={self.p.timeframes}>"
