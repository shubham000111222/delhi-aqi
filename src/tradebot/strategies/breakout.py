"""S3: Donchian 20-bar breakout confirmed by above-average volume."""

from __future__ import annotations

from ..core.models import OrderType, Signal
from . import indicators as ta
from .base import Strategy, StrategyContext


class Breakout(Strategy):
    name = "S3_breakout"

    def __init__(self, params) -> None:
        super().__init__(params)
        self.donchian = self.p.i("donchian", 20)
        self.vol_lookback = self.p.i("vol_lookback", 20)
        self.vol_factor = self.p.f("vol_factor", 1.2)
        self.atr_period = self.p.i("atr_period", 14)
        self.stop_mult = self.p.f("stop_atr_mult", 1.75)
        self.min_bars = max(self.donchian, self.vol_lookback, self.atr_period) + 10
        self.target_r = self.p.f("target_r", 2.5)

    def evaluate(self, ctx: StrategyContext) -> Signal | None:
        close = ctx.series.close
        volume = ctx.series.volume
        if len(close) < self.min_bars:
            return None
        upper, _ = ta.donchian(ctx.series.high, ctx.series.low, self.donchian)
        vol_avg = ta.sma(volume, self.vol_lookback)
        atr = ta.atr(ctx.series.high, ctx.series.low, close, self.atr_period)
        if any(v != v for v in (upper[-1], vol_avg[-1], atr[-1])) or atr[-1] <= 0:
            return None
        if close[-1] <= upper[-1]:
            return None
        if vol_avg[-1] <= 0 or volume[-1] < self.vol_factor * vol_avg[-1]:
            return None
        return self.build_signal(
            ctx,
            stop_distance=atr[-1] * self.stop_mult,
            confidence=min(0.9, 0.45 + (volume[-1] / max(vol_avg[-1], 1e-9) - 1.0) / 4.0),
            reason=(
                f"close {close[-1]:.4g} > Donchian({self.donchian}) {upper[-1]:.4g} on "
                f"{volume[-1] / max(vol_avg[-1], 1e-9):.2f}x average volume"
            ),
            entry_type=OrderType.LIMIT,
            features={
                "donchian_upper": f"{upper[-1]:.4f}",
                "vol_ratio": f"{volume[-1] / max(vol_avg[-1], 1e-9):.2f}",
                "atr": f"{atr[-1]:.4g}",
            },
        )
