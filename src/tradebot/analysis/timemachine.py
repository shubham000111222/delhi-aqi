"""Time Machine foundations: closed-bar replay and standalone block bootstrap.

Not a live gate. Runtime budgets, validation study and integrations are pending.
OHLC cannot establish first touch within a bar: ambiguous replay bars use stop-first.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import overload

import numpy as np
from numpy.typing import NDArray

from ..core.models import Candle


class HistoryError(ValueError):
    pass


@dataclass(frozen=True)
class History:
    bars: tuple[Candle, ...]
    input_hash: str
    # log OHLC relative to the previous close, divided by close-return volatility
    standardized_ohlc: NDArray[np.float64]
    volatility: float


def trade_seed(trade_id: str) -> int:
    if not isinstance(trade_id, str) or not trade_id:
        raise ValueError("trade ID is required")
    return int.from_bytes(hashlib.sha256(trade_id.encode()).digest()[:8], 'big')


def prepare_history(bars: Sequence[Candle], symbol: str, decision_time: datetime,
                    *, days: int = 60, minimum_days: int = 30) -> History:
    """Select only bars closed by decision time, then validate contiguous 5m input.

    Future rows are discarded before value validation/hash; never forward-fill gaps.
    The caller must provide a timezone-aware decision time and actual recorded bars.
    """
    if decision_time.tzinfo is None or decision_time.utcoffset() is None:
        raise HistoryError('decision time must be timezone aware')
    if not 30 <= minimum_days <= days <= 180:
        raise HistoryError('invalid history window')
    cutoff = decision_time - timedelta(days=days)
    selected = []
    for bar in bars:
        if bar.symbol != symbol:
            continue
        if bar.ts.tzinfo is None or bar.ts.utcoffset() is None:
            raise HistoryError('bar timestamp must be timezone aware')
        if bar.ts >= decision_time or bar.ts < cutoff:
            continue
        if bar.timeframe != '5m':
            raise HistoryError('5m bars required')
        if bar.close_time <= decision_time:
            selected.append(bar)
    selected.sort(key=lambda bar: bar.ts)
    if len(selected) < minimum_days * 288:
        raise HistoryError('insufficient closed-bar lookback')
    values = np.array([[float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume)]
                       for b in selected], dtype=np.float64)
    if (not np.isfinite(values).all() or (values[:, :4] <= 0).any() or (values[:, 4] < 0).any()
            or (values[:, 1] < values[:, [0, 2, 3]].max(axis=1)).any()
            or (values[:, 2] > values[:, [0, 1, 3]].min(axis=1)).any()):
        raise HistoryError('invalid OHLCV values')
    if any(b.ts - a.ts != timedelta(minutes=5) for a, b in zip(selected, selected[1:], strict=False)):
        raise HistoryError('duplicate or missing bars')
    if decision_time - selected[-1].close_time >= timedelta(minutes=5):
        raise HistoryError('stale history')
    logs = np.log(values[1:, :4] / values[:-1, 3, None])
    volatility = float(np.std(logs[:, 3], ddof=1))
    if not np.isfinite(volatility) or volatility <= 0:
        raise HistoryError('zero or invalid return volatility')
    # Immutable byte backing prevents downstream code from modifying cached inputs.
    standardized = np.frombuffer((logs / volatility).tobytes(), dtype=np.float64).reshape(-1, 4)
    payload = [[bar.symbol, bar.timeframe, bar.ts.isoformat(), *map(str,
                (bar.open, bar.high, bar.low, bar.close, bar.volume))] for bar in selected]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return History(tuple(selected), digest, standardized, volatility)


@dataclass(frozen=True)
class ReplayPlan:
    entry: float  # decision reference; actual fill is next bar open with adverse slippage
    stop: float
    target: float
    side: int = 1  # +1 long, -1 short
    max_bars: int = 288

    def validate(self) -> None:
        if (self.side not in (-1, 1) or isinstance(self.side, bool)
                or type(self.max_bars) is not int or not 1 <= self.max_bars <= 2016
                or not all(np.isfinite(x) and x > 0 for x in (self.entry, self.stop, self.target))
                or self.side * (self.entry - self.stop) <= 0
                or self.side * (self.target - self.entry) <= 0):
            raise ValueError('invalid replay plan')


class PastBars(Sequence[Candle]):
    """Bounded prefix view: a rule cannot index future observations through this API."""
    def __init__(self, bars: tuple[Candle, ...], end: int) -> None:
        self.__bars = bars
        self.__end = end

    def __len__(self) -> int:
        return self.__end

    @overload
    def __getitem__(self, index: int) -> Candle: ...
    @overload
    def __getitem__(self, index: slice) -> tuple[Candle, ...]: ...
    def __getitem__(self, index: int | slice) -> Candle | tuple[Candle, ...]:
        if isinstance(index, slice):
            return tuple(self.__bars[i] for i in range(*index.indices(self.__end)))
        if index < 0:
            index += self.__end
        if not 0 <= index < self.__end:
            raise IndexError(index)
        return self.__bars[index]

    def __iter__(self) -> Iterator[Candle]:
        return (self.__bars[i] for i in range(self.__end))


def replay_rule(history: History, rule: Callable[[Sequence[Candle]], ReplayPlan | None], *,
                fee_rate: float, slippage_rate: float) -> dict[str, object]:
    """Independent trigger replay, next-open market fills, no overlapping positions.

    Rules see only a closed prefix. Require a fully observed exit horizon before
    evaluating a trigger, avoiding truncated outcomes at the decision boundary.
    Fees apply to both legs; adverse slippage applies to entry/stop/time exits;
    resting targets fill at the target (never invented price improvement).
    """
    if (not np.isfinite(fee_rate) or not np.isfinite(slippage_rate)
            or not 0 <= fee_rate < 1 or not 0 <= slippage_rate < 1):
        raise ValueError('invalid replay costs')
    bars = history.bars
    if len(bars) < 60 * 288:
        raise HistoryError("replay requires at least 60 days of recorded bars")
    outcomes: list[float] = []
    holding: list[int] = []
    counts = {'target_first': 0, 'stop_first': 0, 'time_exit': 0}
    ambiguous = 0
    censored = 0
    i = 0
    while i < len(bars) - 1:
        plan = rule(PastBars(bars, i + 1))
        if plan is None:
            i += 1
            continue
        plan.validate()
        if i + plan.max_bars >= len(bars):
            censored += 1
            i += 1
            continue
        side = plan.side
        entry = float(bars[i + 1].open) * (1 + side * slippage_rate)
        risk = abs(plan.entry - plan.stop)
        result = 'time_exit'
        exit_price = 0.0
        for j in range(i + 1, i + plan.max_bars + 1):
            bar = bars[j]
            op, high, low = float(bar.open), float(bar.high), float(bar.low)
            stop_gap = side * (op - plan.stop) <= 0
            target_gap = side * (op - plan.target) >= 0
            stop_hit = low <= plan.stop if side == 1 else high >= plan.stop
            target_hit = high >= plan.target if side == 1 else low <= plan.target
            if stop_gap or target_gap or stop_hit or target_hit:
                if stop_gap:
                    result, exit_price = 'stop_first', op * (1 - side * slippage_rate)
                elif target_gap:
                    result, exit_price = 'target_first', plan.target
                elif stop_hit:
                    ambiguous += int(target_hit)
                    result, exit_price = 'stop_first', plan.stop * (1 - side * slippage_rate)
                else:
                    result, exit_price = 'target_first', plan.target
                break
            exit_price = float(bar.close) * (1 - side * slippage_rate)
        net = (side * (exit_price - entry) - fee_rate * (entry + exit_price)) / risk
        outcomes.append(net)
        holding.append((j - i) * 5)
        counts[result] += 1
        i = j  # next trigger may be evaluated only after this exit bar closes
    n = len(outcomes)
    return {'triggers': n, 'insufficient': n < 20, 'counts': counts,
            'ambiguous_stop_first': ambiguous, 'censored_triggers': censored,
            'expected_net_r': float(np.mean(outcomes)) if n else None,
            'median_r': float(np.median(outcomes)) if n else None,
            'p05_r': float(np.quantile(outcomes, 0.05)) if n else None,
            'expected_holding_minutes': float(np.mean(holding)) if n else None,
            'input_hash': history.input_hash, 'first_touch_verified': False,
            'limitation': 'OHLC ambiguity uses conservative stop-first; market next-open replay only. '
                          'Rules must not use external future data. Not a Monte Carlo verdict.'}


def simulate_trade(history: History, plan: ReplayPlan, trade_id: str, *,
                   fee_rate: float, slippage_rate: float, paths: int = 10000,
                   volatility_scale: float = 1.0) -> dict[str, object]:
    """Volatility-scaled moving block bootstrap; no market prediction or entry authority.

    Blocks are independently chosen per path with length uniformly 6..12 bars.
    OHLC excursions are sampled jointly. Unknown within-bar high/low ordering is a
    fair random draw when both barriers touch (not inferred execution truth).
    """
    plan.validate()
    if type(paths) is not int or paths not in (5000, 10000):
        raise ValueError('path count must be 5000 or 10000')
    if (not all(np.isfinite(x) for x in (fee_rate, slippage_rate, volatility_scale))
            or not 0 <= fee_rate < 1 or not 0 <= slippage_rate < 1
            or volatility_scale <= 0):
        raise ValueError('invalid simulation costs or volatility scale')
    source = history.standardized_ohlc
    if (len(history.bars) < 30 * 288 or source.shape != (len(history.bars)-1, 4)
            or not np.isfinite(source).all() or not np.isfinite(history.volatility)
            or history.volatility <= 0):
        raise HistoryError('invalid bootstrap history')
    seed = trade_seed(trade_id)
    rng = np.random.default_rng(seed)
    previous = np.full(paths, plan.entry, dtype=np.float64)
    exit_prices = np.zeros(paths)
    duration = np.full(paths, plan.max_bars, dtype=np.int64)
    outcomes = np.zeros(paths, dtype=np.int8)  # 0 time, 1 target, -1 stop
    active = np.ones(paths, dtype=bool)
    indices = np.zeros(paths, dtype=np.int64)
    remaining = np.zeros(paths, dtype=np.int64)
    ambiguous = 0
    side = plan.side
    scale = history.volatility * volatility_scale
    for step in range(plan.max_bars):
        ids = np.flatnonzero(active)
        if not len(ids):
            break
        fresh = ids[remaining[ids] == 0]
        lengths = rng.integers(6, 13, len(fresh))
        # Leave room for the entire block; never wrap past recorded input.
        indices[fresh] = rng.integers(0, len(source)-11, len(fresh))
        remaining[fresh] = lengths
        with np.errstate(over='raise', invalid='raise'):
            sampled = previous[ids, None] * np.exp(source[indices[ids]] * scale)
        op, high, low, close = sampled.T
        if not np.isfinite(sampled).all() or (sampled <= 0).any():
            raise ValueError('invalid simulated prices')
        gap_stop = side * (op - plan.stop) <= 0
        gap_target = side * (op - plan.target) >= 0
        stop_touch = low <= plan.stop if side == 1 else high >= plan.stop
        target_touch = high >= plan.target if side == 1 else low <= plan.target
        both = stop_touch & target_touch & ~gap_stop & ~gap_target
        ambiguous += int(both.sum())
        stop_first = gap_stop | (~gap_target & stop_touch & (~target_touch | (rng.random(len(ids)) < .5)))
        target_first = ~stop_first & (gap_target | target_touch)
        hit = stop_first | target_first
        stop_price = np.where(gap_stop, op, plan.stop) * (1 - side * slippage_rate)
        fills = np.where(stop_first, stop_price, plan.target)
        exited = ids[hit]
        exit_prices[exited] = fills[hit]
        outcomes[exited] = np.where(stop_first[hit], -1, 1)
        duration[exited] = step + 1
        active[exited] = False
        previous[ids] = close
        exit_prices[ids[~hit]] = close[~hit] * (1 - side * slippage_rate)
        indices[ids] += 1
        remaining[ids] -= 1
    entry_fill = plan.entry * (1 + side * slippage_rate)
    risk = abs(plan.entry-plan.stop)
    net = (side * (exit_prices-entry_fill) - fee_rate * (entry_fill+exit_prices)) / risk
    if not np.isfinite(net).all():
        raise ValueError('invalid simulated R outcomes')
    return {'trade_id': trade_id, 'seed': seed, 'path_count': paths,
            'input_hash': history.input_hash,
            'lookback_start': history.bars[0].ts.isoformat(),
            'lookback_end': history.bars[-1].close_time.isoformat(),
            'target_first': float(np.mean(outcomes == 1)),
            'stop_first': float(np.mean(outcomes == -1)),
            'time_exit': float(np.mean(outcomes == 0)),
            'expected_net_r': float(np.mean(net)), 'median_r': float(np.median(net)),
            'p05_r': float(np.quantile(net, .05)),
            'expected_holding_minutes': float(np.mean(duration)*5),
            'ambiguous_order_draws': ambiguous, 'volatility_scale': volatility_scale,
            'first_touch_verified': False,
            'intrabar_model': 'joint OHLC excursions; fair random ordering on dual touches',
            'usable_for_live': False}


def trade_verdict(simulation: dict[str, object], replay: dict[str, object], *,
                  coin_flip_skip: bool = True) -> dict[str, object]:
    """Fixed default thresholds; mandatory skips take precedence. Never risk authority."""
    try:
        def number(key: str) -> float:
            value = simulation[key]
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not np.isfinite(value):
                raise ValueError('invalid simulation metric')
            return float(value)

        mean, tail, timed = number('expected_net_r'), number('p05_r'), number('time_exit')
        target, stop = number('target_first'), number('stop_first')
        if (any(not 0 <= x <= 1 for x in (target, stop, timed))
                or abs(target + stop + timed - 1) > 1e-8):
            raise ValueError('invalid probabilities')
        insufficient = replay['insufficient']
        if type(insufficient) is not bool:
            raise ValueError('invalid replay sufficiency')
        historical = replay.get('expected_net_r')
        if not insufficient and (isinstance(historical, bool) or not isinstance(historical, (float, int))
                                 or not np.isfinite(historical)):
            raise ValueError('invalid replay expectancy')
        coin_flip = abs(mean) < .05
        if mean <= 0 or tail < -1.5 or (timed > .60 and mean < .05):
            verdict, multiplier, reason = 'SKIP', 0.0, 'mandatory expectancy/tail/time-exit rule'
        elif coin_flip and coin_flip_skip:
            verdict, multiplier, reason = 'SKIP', 0.0, 'coin-flip skip setting'
        elif mean < .10 or (not insufficient and float(historical) < 0):  # type: ignore[arg-type]
            verdict, multiplier, reason = 'SIZE DOWN', .5, 'weak expectancy or replay sign disagreement'
        else:
            verdict, multiplier, reason = 'PASS', 1.0, 'simulation threshold met; replay nonnegative or insufficient'
        return {'verdict': verdict, 'multiplier': multiplier, 'label': 'coin flip' if coin_flip else verdict.lower(),
                'reason': reason, 'filter_enabled': False}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {'verdict': 'SKIP', 'multiplier': 0.0, 'label': 'invalid check',
                'reason': 'missing or invalid metrics', 'filter_enabled': False}
