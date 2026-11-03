"""S2: mean reversion on Bollinger bands + RSI, only in low-trend regimes."""

from __future__ import annotations

from ..core.models import OrderType, Regime, Signal
from ..core.money import D
from . import indicators as ta
from .base import Strategy, StrategyContext


class MeanReversion(Strategy):
    name = "S2_meanrev"

    def __init__(self, params) -> None:
        super().__init__(params)
        self.bb_period = self.p.i("bb_period", 20)
        self.bb_stdev = self.p.f("bb_stdev", 2.0)
        self.rsi_period = self.p.i("rsi_period", 14)
        self.rsi_oversold = self.p.f("rsi_oversold", 30)
        self.adx_max = self.p.f("rsi_maxtrend_adx", 22)
        self.atr_period = self.p.i("atr_period", 14)
        self.stop_mult = self.p.f("stop_atr_mult", 1.75)
        self.min_bars = max(self.bb_period, self.rsi_period) + 20
        self.target_r = self.p.f("target_r", 1.5)

    def evaluate(self, ctx: StrategyContext) -> Signal | None:
        close = ctx.series.close
        if len(close) < self.min_bars:
            return None
        if ctx.regime not in (Regime.RANGE, Regime.UNKNOWN):
            return None
        upper, mid, lower = ta.bollinger(close, self.bb_period, self.bb_stdev)
        rsi = ta.rsi(close, self.rsi_period)
        adx, _, _ = ta.adx(ctx.series.high, ctx.series.low, close, self.atr_period)
        atr = ta.atr(ctx.series.high, ctx.series.low, close, self.atr_period)
        vals = (lower[-1], mid[-1], rsi[-1], adx[-1], atr[-1])
        if any(v != v for v in vals) or atr[-1] <= 0:  # NaN guard
            return None
        if adx[-1] > self.adx_max:
            return None
        if not (close[-1] <= lower[-1] and rsi[-1] <= self.rsi_oversold):
            return None

        target = D(mid[-1])
        if target <= ctx.mark:
            return None
        return self.build_signal(
            ctx,
            stop_distance=atr[-1] * self.stop_mult,
            target=target,
            confidence=min(0.85, 0.4 + (self.rsi_oversold - rsi[-1]) / 50.0),
            reason=(
                f"close {close[-1]:.4g} below BB lower {lower[-1]:.4g}, RSI {rsi[-1]:.1f} <= "
                f"{self.rsi_oversold:g}, ADX {adx[-1]:.1f} <= {self.adx_max:g} (range)"
            ),
            entry_type=OrderType.LIMIT,
            features={
                "bb_lower": f"{lower[-1]:.4f}",
                "bb_mid": f"{mid[-1]:.4f}",
                "rsi": f"{rsi[-1]:.1f}",
                "adx": f"{adx[-1]:.1f}",
                "atr": f"{atr[-1]:.4g}",
            },
        )
