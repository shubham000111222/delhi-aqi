"""Market regime detection (trend / range / high volatility).

Strategies declare which regimes they may trade in, so a mean-reversion strategy
simply stops proposing trades in a strong trend instead of being tuned to survive it.
"""

from __future__ import annotations

import numpy as np

from ..config import RegimeConfig
from ..core.models import Regime
from ..data.series import CandleSeries
from . import indicators as ta


def detect_regime(series: CandleSeries, cfg: RegimeConfig | None = None) -> Regime:
    cfg = cfg or RegimeConfig()
    if len(series) < max(cfg.adx_period * 2 + 2, cfg.vol_lookback + 1):
        return Regime.UNKNOWN
    close, high, low = series.close, series.high, series.low
    adx, _, _ = ta.adx(high, low, close, cfg.adx_period)
    rv = ta.realized_vol(close, cfg.vol_lookback)
    if adx[-1] != adx[-1] or rv[-1] != rv[-1]:
        return Regime.UNKNOWN
    pct = ta.percentile_rank(rv, cfg.vol_lookback)
    vol_pct = float(pct[-1]) if pct[-1] == pct[-1] else 0.5
    if vol_pct >= cfg.high_vol_percentile:
        return Regime.HIGH_VOL
    if adx[-1] >= cfg.adx_trend:
        return Regime.TREND
    if adx[-1] <= cfg.adx_range_max:
        return Regime.RANGE
    return Regime.UNKNOWN


def atr_value(series: CandleSeries, period: int = 14) -> float:
    if len(series) < period + 2:
        return float("nan")
    values = ta.atr(series.high, series.low, series.close, period)
    value = ta.last(values)
    return float("nan") if np.isnan(value) else value
