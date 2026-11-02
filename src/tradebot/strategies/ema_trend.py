"""S1: EMA 20/50 trend following with an ADX filter (section 5)."""

from __future__ import annotations

from ..core.models import OrderType, Regime, Signal
from . import indicators as ta
from .base import Strategy, StrategyContext


class EmaTrend(Strategy):
    name = "S1_ema_trend"

    def __init__(self, params) -> None:
        super().__init__(params)
        self.fast = self.p.i("ema_fast", 20)
        self.slow = self.p.i("ema_slow", 50)
        self.adx_min = self.p.f("adx_min", 20)
        self.atr_period = self.p.i("atr_period", 14)
        self.stop_mult = self.p.f("stop_atr_mult", 1.75)
        self.min_bars = max(self.slow + 5, 60)
        self.target_r = self.p.f("target_r", 2.0)
        self.entry_type = OrderType(self.p.raw.get("entry_type", "limit"))
        self.limit_offset_bps = self.p.f("limit_offset_bps", 0.0)

    def evaluate(self, ctx: StrategyContext) -> Signal | None:
        close = ctx.series.close
        if len(close) < self.min_bars:
            return None
        fast = ta.ema(close, self.fast)
        slow = ta.ema(close, self.slow)
        adx, _, _ = ta.adx(ctx.series.high, ctx.series.low, close, self.atr_period)
        atr = ta.atr(ctx.series.high, ctx.series.low, close, self.atr_period)
        if len(fast) < 2 or any(_nan(v) for v in (fast[-1], fast[-2], slow[-1], slow[-2], adx[-1], atr[-1])):
            return None
        if atr[-1] <= 0:
            return None
        crossed_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
        trending = adx[-1] >= self.adx_min and fast[-1] > slow[-1]
        if not (crossed_up or trending):
            return None
        if not self.allows_regime(ctx.regime) and ctx.regime is not Regime.UNKNOWN:
            return None

        entry = ctx.mark
        if self.entry_type is OrderType.LIMIT and self.limit_offset_bps > 0:
            entry = entry * (1 - _dec(self.limit_offset_bps) / _dec(10000))
        return self.build_signal(
            ctx,
            stop_distance=atr[-1] * self.stop_mult,
            entry=entry,
            confidence=min(0.9, 0.5 + (adx[-1] - self.adx_min) / 100.0),
            reason=(
                f"EMA{self.fast}/{self.slow} "
                f"{'cross up' if crossed_up else 'uptrend'}, ADX {adx[-1]:.1f} >= {self.adx_min:g}, "
                f"ATR {atr[-1]:.4g}"
            ),
            entry_type=self.entry_type,
            features={
                "ema_fast": f"{fast[-1]:.4f}",
                "ema_slow": f"{slow[-1]:.4f}",
                "adx": f"{adx[-1]:.1f}",
                "atr": f"{atr[-1]:.4g}",
                "pattern": "cross_up" if crossed_up else "trend_continuation",
            },
        )


def _nan(value: float) -> bool:
    return value != value  # noqa: PLR0124 - NaN check without numpy import


def _dec(value: float):
    from ..core.money import D

    return D(value)
