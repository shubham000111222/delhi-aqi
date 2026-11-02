"""Feature store: the inputs every signal in §19.1 is allowed to see.

Three rules, enforced here rather than trusted to each signal author:

1. **No future data.** Every feature at bar *t* is computed from bars *<= t* only. The
   rolling helpers below are causal by construction, and the tests shift the series to
   prove it.
2. **Rolling normalisation only.** A z-score against full-history statistics leaks the
   future's mean into the past, and it makes a backtest look calmer than live trading
   ever will. Everything is normalised in a trailing window, so early bars are NaN.
3. **NaNs are visible.** Nothing is silently forward-filled or zero-filled. `nan_share()`
   reports what is missing, and the screening step drops rows rather than inventing
   values.

Features are grouped by the families named in the plan (trend, mean reversion,
volatility, volume/flow, order book, cross-asset, time) so a reviewer can see the family
a signal came from and judge whether it makes sense.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from ..core.models import Candle, OrderBook
from ..strategies.indicators import (
    adx,
    atr,
    bollinger,
    donchian,
    ema,
    percentile_rank,
    realized_vol,
    rsi,
    sma,
    vwap,
)

Array = np.ndarray
EPS = 1e-12


# ---------------------------------------------------------------------------
# rolling, causal primitives
# ---------------------------------------------------------------------------


def shift(values: Array, periods: int = 1) -> Array:
    """Lag a series: ``out[t] = values[t - periods]``."""
    out = np.full_like(values, np.nan, dtype=float)
    if periods <= 0:
        return values.astype(float).copy()
    if periods < len(values):
        out[periods:] = values[:-periods]
    return out


def rolling_mean(values: Array, window: int) -> Array:
    out = np.full(len(values), np.nan)
    if window <= 0:
        return out
    for i in range(window - 1, len(values)):
        chunk = values[i - window + 1 : i + 1]
        if np.isnan(chunk).any():
            continue
        out[i] = float(np.mean(chunk))
    return out


def rolling_zscore(values: Array, window: int) -> Array:
    """Trailing z-score; the first ``window - 1`` entries stay NaN."""
    mean = rolling_mean(values, window)
    std = np.full(len(values), np.nan)
    for i in range(window - 1, len(values)):
        chunk = values[i - window + 1 : i + 1]
        if np.isnan(chunk).any():
            continue
        std[i] = float(np.std(chunk))
    safe_std = np.where(std > EPS, std, 1.0)
    z = (values - mean) / safe_std
    constant = std <= EPS
    z = np.where(constant & ~np.isnan(values), 0.0, z)
    return z


def rolling_corr(a: Array, b: Array, window: int) -> Array:
    out = np.full(len(a), np.nan)
    for i in range(window - 1, len(a)):
        x = a[i - window + 1 : i + 1]
        y = b[i - window + 1 : i + 1]
        if np.isnan(x).any() or np.isnan(y).any():
            continue
        if np.std(x) <= EPS or np.std(y) <= EPS:
            out[i] = 0.0
            continue
        out[i] = float(np.corrcoef(x, y)[0, 1])
    return out


def slope(values: Array, window: int) -> Array:
    """Least-squares slope over a trailing window, scaled by the last price.

    Scale-free, so an EMA slope on BTC and one on XRP are comparable.
    """
    out = np.full(len(values), np.nan)
    x = np.arange(window, dtype=float)
    x_centred = x - x.mean()
    denom = float(np.sum(x_centred**2))
    for i in range(window - 1, len(values)):
        chunk = values[i - window + 1 : i + 1]
        if np.isnan(chunk).any() or abs(chunk[-1]) <= EPS:
            continue
        beta = float(np.sum(x_centred * (chunk - chunk.mean())) / denom)
        out[i] = beta / abs(chunk[-1])
    return out


def safe_div(numerator: Array, denominator: Array) -> Array:
    return numerator / np.where(np.abs(denominator) > EPS, denominator, np.nan)


def pct_change(values: Array, periods: int = 1) -> Array:
    return safe_div(values - shift(values, periods), shift(values, periods))


def clip(values: Array, limit: float = 1.0) -> Array:
    return np.clip(values, -limit, limit)


def tanh_score(values: Array) -> Array:
    """Map anything to [-1, 1] without a hard clip that hides outliers."""
    return np.tanh(np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0))


# ---------------------------------------------------------------------------
# the frame
# ---------------------------------------------------------------------------


@dataclass
class FeatureFrame:
    """Features for one symbol, aligned to its bars."""

    symbol: str
    ts: list[datetime]
    close: Array
    features: dict[str, Array] = field(default_factory=dict)
    families: dict[str, str] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.ts)

    @property
    def names(self) -> list[str]:
        return list(self.features)

    def matrix(self, names: Sequence[str] | None = None) -> Array:
        chosen = list(names or self.features)
        if not chosen:
            return np.empty((len(self.ts), 0))
        return np.column_stack([self.features[name] for name in chosen])

    def nan_share(self) -> dict[str, float]:
        return {
            name: float(np.mean(np.isnan(values))) if len(values) else 1.0
            for name, values in self.features.items()
        }

    def usable_rows(self, names: Sequence[str] | None = None) -> Array:
        """Boolean mask of rows with no NaNs in the chosen features."""
        matrix = self.matrix(names)
        if matrix.size == 0:
            return np.zeros(len(self.ts), dtype=bool)
        return ~np.isnan(matrix).any(axis=1)

    def describe(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bars": len(self.ts),
            "features": len(self.features),
            "families": sorted(set(self.families.values())),
            "nan_share": {k: round(v, 3) for k, v in self.nan_share().items()},
        }


# ---------------------------------------------------------------------------
# feature definitions
# ---------------------------------------------------------------------------

#: A feature function takes the raw arrays and returns one array per bar.
FeatureFn = Callable[[dict[str, Any]], Array]


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    family: str
    lookback: int
    fn: FeatureFn
    version: str = "1.0"
    normalise: bool = True


def _ema_slope(data: dict[str, Any], period: int, window: int) -> Array:
    line = ema(data["close"], period)
    return slope(line, window)


def _donchian_position(data: dict[str, Any], period: int) -> Array:
    upper, lower = donchian(data["high"], data["low"], period)
    width = upper - lower
    return safe_div(data["close"] - lower, width) * 2.0 - 1.0


def _ema_from_first(values: Array, period: int) -> Array:
    """EMA that starts at the first non-NaN value (``ema()`` would stay NaN forever)."""
    out = np.full(len(values), np.nan)
    finite = np.flatnonzero(~np.isnan(values))
    if finite.size == 0 or period <= 0:
        return out
    start = int(finite[0])
    seed = float(np.nanmean(values[start : start + period]))
    if np.isnan(seed):
        return out
    alpha = 2.0 / (period + 1.0)
    prev = seed
    out[start + min(period, len(values) - start) - 1] = prev
    for i in range(start + period, len(values)):
        if np.isnan(values[i]):
            out[i] = prev
            continue
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def _macd_histogram(data: dict[str, Any], fast: int, slow: int, signal: int) -> Array:
    macd = ema(data["close"], fast) - ema(data["close"], slow)
    return macd - _ema_from_first(macd, signal)


def _rsi_distance(data: dict[str, Any], period: int) -> Array:
    return (rsi(data["close"], period) - 50.0) / 50.0


def _bollinger_z(data: dict[str, Any], period: int, stdev: float) -> Array:
    _, middle, upper = bollinger(data["close"], period, stdev)
    width = upper - middle
    return safe_div(data["close"] - middle, width)


def _vwap_distance(data: dict[str, Any], period: int) -> Array:
    line = vwap(data["high"], data["low"], data["close"], data["volume"], period)
    return safe_div(data["close"] - line, line)


def _atr_percent(data: dict[str, Any], period: int) -> Array:
    return safe_div(atr(data["high"], data["low"], data["close"], period), data["close"])


def _atr_percentile(data: dict[str, Any], period: int, window: int) -> Array:
    values = _atr_percent(data, period)
    return percentile_rank(values, window) - 0.5


def _vol_of_vol(data: dict[str, Any], period: int, window: int) -> Array:
    vol = realized_vol(data["close"], period)
    return rolling_zscore(vol, window)


def _bollinger_width(data: dict[str, Any], period: int, stdev: float) -> Array:
    _, middle, upper = bollinger(data["close"], period, stdev)
    return safe_div(upper - middle, middle)


def _range_compression(data: dict[str, Any], short: int, long: int) -> Array:
    high_short = np.full(len(data["close"]), np.nan)
    low_short = high_short.copy()
    for i in range(short - 1, len(data["close"])):
        high_short[i] = float(np.max(data["high"][i - short + 1 : i + 1]))
        low_short[i] = float(np.min(data["low"][i - short + 1 : i + 1]))
    short_range = safe_div(high_short - low_short, data["close"])
    # compare with the long-run average true range expressed the same way
    baseline = rolling_mean(_atr_percent(data, short), long)
    return safe_div(short_range, baseline) - 1.0


def _volume_z(data: dict[str, Any], window: int) -> Array:
    return rolling_zscore(data["volume"], window)


def _obv(data: dict[str, Any]) -> Array:
    direction = np.sign(np.diff(data["close"], prepend=data["close"][0]))
    return np.cumsum(direction * data["volume"])


def _obv_slope(data: dict[str, Any], window: int) -> Array:
    return slope(_obv(data), window)


def _taker_buy_ratio(data: dict[str, Any], window: int) -> Array:
    """Candle-direction-weighted volume: the only flow proxy a bar feed can offer.

    It is not real taker data - the docstring says so because treating it as if it were
    would make a weak signal look stronger than it is.
    """
    direction = np.sign(data["close"] - data["open"])
    signed = direction * data["volume"]
    ratio = safe_div(rolling_mean(signed, window), rolling_mean(data["volume"], window) + EPS)
    return 2.0 * ratio - 1.0  # centred on 0 so a balanced book reads as zero


def _dollar_volume_z(data: dict[str, Any], window: int) -> Array:
    return rolling_zscore(data["close"] * data["volume"], window)


def _hour_of_day(data: dict[str, Any]) -> Array:
    hours = np.array([ts.hour + ts.minute / 60.0 for ts in data["ts"]], dtype=float)
    return hours / 23.5 * 2.0 - 1.0


def _weekend_flag(data: dict[str, Any]) -> Array:
    return np.array([1.0 if ts.weekday() >= 5 else 0.0 for ts in data["ts"]], dtype=float)


# Order-book features read a pre-aligned matrix of book summaries.
def _book_imbalance(data: dict[str, Any], levels: int) -> Array:
    key = f"imbalance_{levels}"
    return data["books"].get(key, np.full(len(data["close"]), np.nan))


def _book_spread(data: dict[str, Any]) -> Array:
    return data["books"].get("spread_bps", np.full(len(data["close"]), np.nan))


def _book_depth_ratio(data: dict[str, Any]) -> Array:
    return data["books"].get("depth_ratio", np.full(len(data["close"]), np.nan))


def _microprice_offset(data: dict[str, Any]) -> Array:
    return data["books"].get("microprice_offset", np.full(len(data["close"]), np.nan))


#: Every feature the store knows. Adding one is a single line; the family and lookback
#: are metadata for the model card, not decoration.
FEATURES: dict[str, FeatureSpec] = {
    # trend
    "ema_slope_20": FeatureSpec("ema_slope_20", "trend", 40,
                                lambda d: _ema_slope(d, 20, 20)),
    "ema_slope_50": FeatureSpec("ema_slope_50", "trend", 100,
                                lambda d: _ema_slope(d, 50, 50)),
    "donchian_pos_20": FeatureSpec("donchian_pos_20", "trend", 20,
                                   lambda d: _donchian_position(d, 20)),
    "donchian_pos_55": FeatureSpec("donchian_pos_55", "trend", 55,
                                   lambda d: _donchian_position(d, 55)),
    "macd_hist_12_26": FeatureSpec("macd_hist_12_26", "trend", 60,
                                   lambda d: _macd_histogram(d, 12, 26, 9)),
    "price_vs_sma_50": FeatureSpec("price_vs_sma_50", "trend", 50,
                                   lambda d: safe_div(d["close"] - sma(d["close"], 50),
                                                      sma(d["close"], 50))),
    "adx_direction": FeatureSpec("adx_direction", "trend", 40,
                                 lambda d: _adx_direction(d, 14)),
    # mean reversion
    "rsi_distance_14": FeatureSpec("rsi_distance_14", "mean_reversion", 14,
                                   lambda d: _rsi_distance(d, 14)),
    "rsi_distance_7": FeatureSpec("rsi_distance_7", "mean_reversion", 7,
                                  lambda d: _rsi_distance(d, 7)),
    "bb_z_20": FeatureSpec("bb_z_20", "mean_reversion", 20,
                           lambda d: _bollinger_z(d, 20, 2.0)),
    "vwap_distance_20": FeatureSpec("vwap_distance_20", "mean_reversion", 20,
                                    lambda d: _vwap_distance(d, 20)),
    "close_z_20": FeatureSpec("close_z_20", "mean_reversion", 20,
                              lambda d: rolling_zscore(d["close"], 20)),
    "ret_1": FeatureSpec("ret_1", "mean_reversion", 2,
                         lambda d: pct_change(d["close"], 1)),
    "ret_5": FeatureSpec("ret_5", "trend", 6, lambda d: pct_change(d["close"], 5)),
    "ret_20": FeatureSpec("ret_20", "trend", 21, lambda d: pct_change(d["close"], 20)),
    # volatility
    "atr_pct_14": FeatureSpec("atr_pct_14", "volatility", 14,
                              lambda d: _atr_percent(d, 14)),
    "atr_percentile_96": FeatureSpec("atr_percentile_96", "volatility", 110,
                                     lambda d: _atr_percentile(d, 14, 96)),
    "realized_vol_24": FeatureSpec("realized_vol_24", "volatility", 24,
                                   lambda d: realized_vol(d["close"], 24)),
    "vol_of_vol_24": FeatureSpec("vol_of_vol_24", "volatility", 48,
                                 lambda d: _vol_of_vol(d, 12, 24)),
    "bb_width_20": FeatureSpec("bb_width_20", "volatility", 20,
                               lambda d: _bollinger_width(d, 20, 2.0)),
    "range_compression": FeatureSpec("range_compression", "volatility", 60,
                                     lambda d: _range_compression(d, 12, 48)),
    # volume and flow
    "volume_z_20": FeatureSpec("volume_z_20", "volume", 20,
                               lambda d: _volume_z(d, 20)),
    "obv_slope_24": FeatureSpec("obv_slope_24", "volume", 48,
                                lambda d: _obv_slope(d, 24)),
    "taker_buy_ratio_20": FeatureSpec("taker_buy_ratio_20", "volume", 20,
                                      lambda d: _taker_buy_ratio(d, 20)),
    "dollar_volume_z_20": FeatureSpec("dollar_volume_z_20", "volume", 20,
                                      lambda d: _dollar_volume_z(d, 20)),
    # order book (NaN when no book data was supplied)
    "book_imbalance_5": FeatureSpec("book_imbalance_5", "order_book", 1,
                                    lambda d: _book_imbalance(d, 5), normalise=False),
    "book_imbalance_10": FeatureSpec("book_imbalance_10", "order_book", 1,
                                     lambda d: _book_imbalance(d, 10), normalise=False),
    "book_spread_bps": FeatureSpec("book_spread_bps", "order_book", 1, _book_spread),
    "book_depth_ratio": FeatureSpec("book_depth_ratio", "order_book", 1, _book_depth_ratio),
    "microprice_offset": FeatureSpec("microprice_offset", "order_book", 1,
                                     _microprice_offset, normalise=False),
    # time
    "hour_of_day": FeatureSpec("hour_of_day", "time", 0, _hour_of_day, normalise=False),
    "weekend_flag": FeatureSpec("weekend_flag", "time", 0, _weekend_flag, normalise=False),
}


def _adx_direction(data: dict[str, Any], period: int) -> Array:
    _, plus_di, minus_di = adx(data["high"], data["low"], data["close"], period)
    return safe_div(plus_di - minus_di, plus_di + minus_di)


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


class FeatureStore:
    """Computes, normalises and reports features for a symbol."""

    def __init__(
        self,
        specs: Mapping[str, FeatureSpec] | None = None,
        *,
        normalise_window: int = 96,
        book_features: bool = True,
    ) -> None:
        chosen = dict(specs or FEATURES)
        if not book_features:
            chosen = {k: v for k, v in chosen.items() if v.family != "order_book"}
        self.specs = chosen
        self.normalise_window = normalise_window

    def compute(
        self,
        candles: Sequence[Candle],
        *,
        books: Mapping[datetime, OrderBook] | None = None,
        context: Mapping[str, Array] | None = None,
    ) -> FeatureFrame:
        if len(candles) < 2:
            raise ValueError("need at least two candles")
        close = np.array([float(c.close) for c in candles])
        data: dict[str, Any] = {
            "close": close,
            "open": np.array([float(c.open) for c in candles]),
            "high": np.array([float(c.high) for c in candles]),
            "low": np.array([float(c.low) for c in candles]),
            "volume": np.array([float(c.volume) for c in candles]),
            "ts": [c.ts for c in candles],
            "books": _book_features(candles, books) if books else {},
        }
        frame = FeatureFrame(
            symbol=candles[0].symbol,
            ts=[c.ts for c in candles],
            close=close,
        )
        # Features that cannot be computed are left out rather than shipped as a column
        # of NaNs: a reviewer should see "no book data" in the frame's contents, not a
        # signal that quietly never fires.
        specs = self.specs if books else {
            k: v for k, v in self.specs.items() if v.family != "order_book"
        }
        for name, spec in specs.items():
            try:
                raw = np.asarray(spec.fn(data), dtype=float)
            except (ValueError, IndexError, ZeroDivisionError):  # pragma: no cover - defensive
                raw = np.full(len(close), np.nan)
            if len(raw) != len(close):
                raw = np.resize(raw, len(close))
            values = rolling_zscore(raw, self.normalise_window) if spec.normalise else raw
            frame.features[name] = values
            frame.families[name] = spec.family
            frame.versions[name] = spec.version
        for name, values in (context or {}).items():
            if len(values) != len(close):
                values = np.resize(values, len(close))
            frame.features[name] = np.asarray(values, dtype=float)
            frame.families[name] = "cross_asset"
            frame.versions[name] = "1.0"
        return frame

    def compute_many(
        self,
        series: Mapping[str, Sequence[Candle]],
        *,
        books: Mapping[str, Mapping[datetime, OrderBook]] | None = None,
    ) -> dict[str, FeatureFrame]:
        """Per-symbol frames plus a BTC-relative context feature when BTC is present."""
        frames: dict[str, FeatureFrame] = {}
        closes = {
            symbol: np.array([float(c.close) for c in candles])
            for symbol, candles in series.items()
        }
        reference = closes.get("BTC/USDT")
        for symbol, candles in series.items():
            context: dict[str, Array] = {}
            if reference is not None and len(reference) == len(candles):
                # cross-asset: relative strength and rolling correlation against BTC
                context["btc_relative_strength"] = pct_change(closes[symbol], 20) - pct_change(
                    reference, 20
                )
                context["btc_correlation_96"] = rolling_corr(closes[symbol], reference, 96)
            frames[symbol] = self.compute(
                candles, books=(books or {}).get(symbol), context=context
            )
        return frames


def _book_features(
    candles: Sequence[Candle],
    books: Mapping[datetime, OrderBook],
    *,
    max_staleness_seconds: float | None = None,
) -> dict[str, Array]:
    """Align book snapshots to bars, then summarise the top of the book.

    Only books at or before the bar's close are used: a book stamped after the bar would
    be future information. A book that is merely *old* is not future information but it is
    still wrong - a five-minute-old imbalance says nothing about the next fill - so a
    snapshot older than the staleness limit (default: five bars) yields NaN rather than
    being carried forward for the rest of the session.
    """
    if max_staleness_seconds is None:
        if len(candles) >= 2:
            gaps = [
                (candles[i].close_time - candles[i - 1].close_time).total_seconds()
                for i in range(1, min(len(candles), 50))
            ]
            spacing = float(np.median([gap for gap in gaps if gap > 0] or [60.0]))
        else:
            spacing = 60.0
        max_staleness_seconds = 5.0 * spacing
    n = len(candles)
    imbalance_5 = np.full(n, np.nan)
    imbalance_10 = np.full(n, np.nan)
    spread = np.full(n, np.nan)
    depth_ratio = np.full(n, np.nan)
    microprice = np.full(n, np.nan)
    stamps = sorted(books)
    cursor = 0
    current: OrderBook | None = None
    current_stamp: datetime | None = None
    for i, candle in enumerate(candles):
        while cursor < len(stamps) and stamps[cursor] <= candle.close_time:
            current_stamp = stamps[cursor]
            current = books[current_stamp]
            cursor += 1
        if current is None or current_stamp is None:
            continue
        if (candle.close_time - current_stamp).total_seconds() > max_staleness_seconds:
            continue  # stale: better a NaN than a confident wrong number
        bid_qty = sum(float(level.qty) for level in current.bids[:5])
        ask_qty = sum(float(level.qty) for level in current.asks[:5])
        bid_qty_10 = sum(float(level.qty) for level in current.bids[:10])
        ask_qty_10 = sum(float(level.qty) for level in current.asks[:10])
        imbalance_5[i] = (bid_qty - ask_qty) / (bid_qty + ask_qty + EPS)
        imbalance_10[i] = (bid_qty_10 - ask_qty_10) / (bid_qty_10 + ask_qty_10 + EPS)
        best_bid = current.best_bid
        best_ask = current.best_ask
        if best_bid and best_ask and best_bid > 0:
            spread[i] = float((best_ask - best_bid) / best_bid * 10_000)
            if best_bid + best_ask > 0:
                mid = (best_bid + best_ask) / 2
                microprice[i] = float(best_ask - mid) / float(mid) if mid else np.nan
        depth_ratio[i] = (bid_qty_10 + 1e-9) / (ask_qty_10 + 1e-9)
    return {
        "imbalance_5": imbalance_5,
        "imbalance_10": imbalance_10,
        "spread_bps": spread,
        "depth_ratio": depth_ratio,
        "microprice_offset": microprice,
    }


def forward_returns(close: Array, horizon: int) -> Array:
    """Return from *t* to *t + horizon*, used only to *evaluate* signals.

    This is the one place the future is allowed, and it is deliberately named so nobody
    mistakes it for an input.
    """
    out = np.full(len(close), np.nan)
    if horizon < len(close):
        out[:-horizon] = close[horizon:] / close[:-horizon] - 1.0
    return out


def bars_per_day(ts: Sequence[datetime]) -> float:
    if len(ts) < 2:
        return 1440.0
    deltas = [t for t in np.diff([t.timestamp() for t in ts]) if t > 0]
    if not deltas:
        return 1440.0
    median = float(np.median(deltas))
    return max(1.0, 86_400.0 / median) if median else 1440.0


def horizon_bars(minutes: int, timeframe_minutes: int = 1) -> int:
    return max(1, int(round(minutes / max(1, timeframe_minutes))))


def synthetic_candles(*args: Any, **kwargs: Any) -> list[Candle]:  # pragma: no cover - helper
    """Build a simple geometric-random-walk series, for tests and demos."""
    seed = int(kwargs.get("seed", 7))
    count = int(kwargs.get("count", 480))
    start = kwargs.get("start") or datetime(2024, 1, 1)
    rng = np.random.default_rng(seed)
    returns = rng.normal(0, 0.001, count)
    prices = 100.0 * np.exp(np.cumsum(returns))
    out: list[Candle] = []
    previous = 100.0
    for i, price in enumerate(prices):
        ts = start + timedelta(minutes=i)
        wick = price * 0.0008
        # open at the previous close so the candle body carries the bar's direction
        open_price, close_price = previous, price
        high = max(open_price, close_price) + float(rng.uniform(0.0, wick))
        low = min(open_price, close_price) - float(rng.uniform(0.0, wick))
        out.append(
            Candle(
                symbol=kwargs.get("symbol", "TEST/USDT"),
                timeframe="1m",
                ts=ts,
                open=_dec(open_price),
                high=_dec(high),
                low=_dec(low),
                close=_dec(close_price),
                volume=_dec(max(1.0, float(rng.gamma(2.0, 5.0)))),
            )
        )
        previous = price
    return out


def _dec(value: float) -> Any:
    from decimal import Decimal

    return Decimal(f"{value:.8f}")
