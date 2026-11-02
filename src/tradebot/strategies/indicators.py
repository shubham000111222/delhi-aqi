"""Indicator math on plain numpy arrays.

Kept dependency-free (no TA-Lib, no pandas required) and fully deterministic so a
backtest, a replay and live produce identical numbers from identical candles.
Every function returns arrays of the same length as the input, left-padded with NaN.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

Array = NDArray[np.float64]


def _as_array(values: Array | list[float] | tuple[float, ...]) -> Array:
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError("expected a 1-D array")
    return arr


def nans(n: int) -> Array:
    return np.full(n, np.nan, dtype=np.float64)


def sma(values: Array, period: int) -> Array:
    x = _as_array(values)
    n = len(x)
    out = nans(n)
    if period <= 0 or n < period:
        return out
    csum = np.cumsum(np.insert(x, 0, 0.0))
    out[period - 1 :] = (csum[period:] - csum[:-period]) / period
    return out


def ema(values: Array, period: int) -> Array:
    x = _as_array(values)
    n = len(x)
    out = nans(n)
    if period <= 0 or n < period:
        return out
    alpha = 2.0 / (period + 1.0)
    prev = float(np.mean(x[:period]))
    out[period - 1] = prev
    for i in range(period, n):
        prev = alpha * x[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def rma(values: Array, period: int) -> Array:
    """Wilder's smoothing (used by RSI and ATR)."""
    x = _as_array(values)
    n = len(x)
    out = nans(n)
    if period <= 0 or n < period:
        return out
    prev = float(np.mean(x[:period]))
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + x[i]) / period
        out[i] = prev
    return out


def true_range(high: Array, low: Array, close: Array) -> Array:
    h, l, c = _as_array(high), _as_array(low), _as_array(close)
    n = len(c)
    tr = nans(n)
    if n == 0:
        return tr
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return tr


def atr(high: Array, low: Array, close: Array, period: int = 14) -> Array:
    return rma(true_range(high, low, close), period)


def rsi(close: Array, period: int = 14) -> Array:
    c = _as_array(close)
    n = len(c)
    out = nans(n)
    if n < period + 1:
        return out
    delta = np.diff(c, prepend=c[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = rma(gain[1:], period)
    avg_loss = rma(loss[1:], period)
    for i in range(period, n):
        g = avg_gain[i - 1]
        l_ = avg_loss[i - 1]
        if np.isnan(g) or np.isnan(l_):
            continue
        out[i] = 100.0 if l_ == 0 else 100.0 - 100.0 / (1.0 + g / l_)
    return out


def adx(high: Array, low: Array, close: Array, period: int = 14) -> tuple[Array, Array, Array]:
    """Returns (adx, plus_di, minus_di). Wilder's smoothing, NaN until warm."""
    h, l, c = _as_array(high), _as_array(low), _as_array(close)
    n = len(c)
    adx_out, pdi_out, mdi_out = nans(n), nans(n), nans(n)
    if n < period * 2:
        return adx_out, pdi_out, mdi_out
    up = np.zeros(n)
    down = np.zeros(n)
    for i in range(1, n):
        up_move = h[i] - h[i - 1]
        down_move = l[i - 1] - l[i]
        up[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        down[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
    tr = true_range(h, l, c)
    tr_s = rma(tr[1:], period)
    up_s = rma(up[1:], period)
    down_s = rma(down[1:], period)
    dx = nans(n)
    for i in range(period - 1, n - 1):
        if np.isnan(tr_s[i]) or tr_s[i] == 0:
            continue
        pdi = 100.0 * up_s[i] / tr_s[i]
        mdi = 100.0 * down_s[i] / tr_s[i]
        pdi_out[i + 1] = pdi
        mdi_out[i + 1] = mdi
        denom = pdi + mdi
        dx[i + 1] = 100.0 * abs(pdi - mdi) / denom if denom else 0.0
    valid_idx = np.flatnonzero(~np.isnan(dx))
    if valid_idx.size >= period:
        first = int(valid_idx[0])
        adx_out[first:] = rma(dx[first:], period)
    return adx_out, pdi_out, mdi_out


def rolling_std(values: Array, period: int) -> Array:
    x = _as_array(values)
    n = len(x)
    out = nans(n)
    for i in range(period - 1, n):
        out[i] = float(np.std(x[i - period + 1 : i + 1], ddof=0))
    return out


def bollinger(close: Array, period: int = 20, stdev: float = 2.0) -> tuple[Array, Array, Array]:
    c = _as_array(close)
    mid = sma(c, period)
    sd = rolling_std(c, period)
    return mid + stdev * sd, mid, mid - stdev * sd


def donchian(high: Array, low: Array, period: int = 20) -> tuple[Array, Array]:
    """Upper/lower channel of the *previous* `period` bars (excludes the current bar)."""
    h, l = _as_array(high), _as_array(low)
    n = len(h)
    upper, lower = nans(n), nans(n)
    for i in range(period, n):
        upper[i] = float(np.max(h[i - period : i]))
        lower[i] = float(np.min(l[i - period : i]))
    return upper, lower


def realized_vol(close: Array, period: int = 96) -> Array:
    c = _as_array(close)
    n = len(c)
    out = nans(n)
    if n < 3:
        return out
    logret = np.zeros(n)
    with np.errstate(divide="ignore", invalid="ignore"):
        logret[1:] = np.log(c[1:] / c[:-1])
    for i in range(period, n):
        out[i] = float(np.std(logret[i - period + 1 : i + 1], ddof=0))
    return out


def percentile_rank(values: Array, window: int) -> Array:
    """Rank of the latest value within its own trailing window, in [0, 1]."""
    x = _as_array(values)
    n = len(x)
    out = nans(n)
    for i in range(window - 1, n):
        chunk = x[i - window + 1 : i + 1]
        clean = chunk[~np.isnan(chunk)]
        if clean.size == 0:
            continue
        out[i] = float((clean <= x[i]).sum()) / clean.size
    return out


def zscore(values: Array, period: int) -> Array:
    x = _as_array(values)
    n = len(x)
    out = nans(n)
    mean = sma(x, period)
    sd = rolling_std(x, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(sd > 0, (x - mean) / sd, 0.0)
    out[: period - 1] = np.nan
    return out


def vwap(high: Array, low: Array, close: Array, volume: Array, period: int = 20) -> Array:
    h, l, c, v = _as_array(high), _as_array(low), _as_array(close), _as_array(volume)
    typical = (h + l + c) / 3.0
    n = len(c)
    out = nans(n)
    for i in range(period - 1, n):
        vv = v[i - period + 1 : i + 1]
        total = float(vv.sum())
        if total <= 0:
            out[i] = float(np.mean(typical[i - period + 1 : i + 1]))
        else:
            out[i] = float((typical[i - period + 1 : i + 1] * vv).sum() / total)
    return out


def last(arr: Array) -> float:
    """Last non-NaN value, or NaN."""
    for value in arr[::-1]:
        if not np.isnan(value):
            return float(value)
    return float("nan")
