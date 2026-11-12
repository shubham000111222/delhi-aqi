"""Time Machine foundations: closed-bar replay and standalone block bootstrap.

Not a live gate. Runtime budgets, validation study and integrations are pending.
OHLC cannot establish first touch within a bar: ambiguous replay bars use stop-first.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import overload

import numpy as np
from numpy.typing import NDArray

from ..core.clock import floor_ts, utc
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
        if bar.ts != floor_ts(bar.ts, 300):
            raise HistoryError('5m timestamps must align to UTC five-minute boundaries')
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
    payload = [[bar.symbol, bar.timeframe, utc(bar.ts).isoformat(), *map(str,
                (bar.open, bar.high, bar.low, bar.close, bar.volume))] for bar in selected]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return History(tuple(selected), digest, standardized, volatility)


def current_volatility_scale(history: History) -> dict[str, object]:
    """Fixed trailing24h close-return estimator; not tuned to a holdout outcome.

    The supplied History must already be prepared/validated at the decision cutoff.
    Standardized returns use historical sigma, so their recent sample sigma is the
    required current/historical scale ratio. No future window or forward fill.
    """
    recent = history.standardized_ohlc[-288:, 3]
    if len(recent) != 288 or not np.isfinite(recent).all():
        raise HistoryError('invalid current-volatility return window')
    scale = float(np.std(recent, ddof=1))
    current = scale * history.volatility
    if not np.isfinite(scale) or scale <= 0 or not np.isfinite(current) or current <= 0:
        raise HistoryError('zero or invalid current volatility')
    return {'estimator': 'trailing288-closed5m-close-log-returns-ddof1-v1',
            'return_observations': 288, 'current_volatility': current,
            'baseline_volatility': history.volatility, 'scale': scale,
            'window_end': history.bars[-1].close_time.isoformat()}


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
                fee_rate: float, slippage_rate: float, deadline: float | None = None) -> dict[str, object]:
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
        if deadline is not None and time.perf_counter() >= deadline:
            raise TimeoutError("replay budget exceeded")
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
                   volatility_scale: float = 1.0, deadline: float | None = None) -> dict[str, object]:
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
        if deadline is not None and time.perf_counter() >= deadline:
            raise TimeoutError("simulation budget exceeded")
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


def replay_only_verdict(replay: dict[str, object], *, coin_flip_skip: bool = True) -> dict[str, object]:
    """Predeclared research ablation: same thresholds on observed replay metrics.

    Unlike combined mode, insufficient replay alone cannot support an entry.
    This is not a new live policy or a calibrated strategy-selection rule.
    """
    try:
        if replay.get('insufficient') is not False:
            raise ValueError('insufficient replay-only evidence')
        counts = replay['counts']
        if not isinstance(counts, dict):
            raise ValueError('invalid replay counts')
        values = [counts[k] for k in ('target_first', 'stop_first', 'time_exit')]
        if any(type(v) is not int or v < 0 for v in values):
            raise ValueError('invalid replay counts')
        total = sum(values)
        if total < 20 or replay.get('triggers') != total:
            raise ValueError('insufficient or inconsistent replay counts')
        metrics = {'expected_net_r': replay.get('expected_net_r'), 'p05_r': replay.get('p05_r'),
                   **{k: v/total for k, v in zip(('target_first', 'stop_first', 'time_exit'), values, strict=True)}}
        result = trade_verdict(metrics, {'insufficient': True}, coin_flip_skip=coin_flip_skip)
        result['reason'] = 'replay-only thresholds: ' + str(result['reason'])
        return result
    except (KeyError, TypeError, ValueError, OverflowError):
        return {'verdict': 'SKIP', 'multiplier': 0., 'label': 'insufficient or invalid replay',
                'reason': 'replay-only requires at least 20 valid completed triggers', 'filter_enabled': False}


class TimeMachine:
    """Standalone, disabled-by-default check with bounded process-local caches.

    Alert callback is required; delivery failure is recorded and still skips. No
    persistence, risk-engine mutation or production execution authority is provided.
    Deadlines are cooperative, not a hard wall-clock preemption guarantee.
    """
    def __init__(self, alert: Callable[[str], None], *, budget_ms: float = 250,
                 cache_entries: int = 2) -> None:
        if (not callable(alert) or not np.isfinite(budget_ms) or not 0 < budget_ms <= 250
                or type(cache_entries) is not int or not 1 <= cache_entries <= 4):
            raise ValueError('invalid Time Machine configuration')
        self.alert = alert
        self.budget_ms = budget_ms
        self.cache_entries = cache_entries
        self.paths = 10000
        self.overruns = 0
        self._history_cache: OrderedDict[object, History] = OrderedDict()
        self._replay_cache: OrderedDict[object, dict[str, object]] = OrderedDict()

    def _notify(self, message: str) -> bool:
        try:
            self.alert(message)
            return True
        except Exception:  # noqa: BLE001 - alert failure cannot allow a trade
            return False

    def _prepared(self, bars: Sequence[Candle], symbol: str, decision_time: datetime,
                  days: int, *, minimum_days: int = 30) -> History:
        closed = tuple(b for b in bars if b.symbol == symbol
                       and decision_time - timedelta(days=days) <= b.ts < decision_time
                       and b.close_time <= decision_time)
        key = (symbol, decision_time, days, minimum_days, closed)
        history = self._history_cache.get(key)
        if history is None:
            history = prepare_history(closed, symbol, decision_time, days=days, minimum_days=minimum_days)
            self._history_cache[key] = history
            if len(self._history_cache) > self.cache_entries:
                self._history_cache.popitem(last=False)
        self._history_cache.move_to_end(key)
        return history

    def check(self, bars: Sequence[Candle] | Callable[[], Sequence[Candle]], symbol: str, decision_time: datetime,
              plan: ReplayPlan, trade_id: str, rule: Callable[[Sequence[Candle]], ReplayPlan | None], *,
              fee_rate: float, slippage_rate: float, rule_version: str, volatility_scale: float | None = 1.,
              coin_flip_skip: bool = True, variant: str = 'both', replay_days: int = 60) -> dict[str, object]:
        started = time.perf_counter()
        deadline = started + self.budget_ms / 1000
        paths = self.paths
        result: dict[str, object] = {'trade_id': trade_id, 'path_count': 0 if variant == 'replay_only' else paths,
                                    'verdict': 'SKIP', 'multiplier': 0., 'filter_enabled': False, 'variant': variant}
        try:
            if variant not in ('both', 'simulation_only', 'replay_only'):
                raise ValueError('unknown research check variant')
            if type(replay_days) is not int or not 60 <= replay_days <= 180:
                raise ValueError('replay lookback must be60..180 days')
            result.update(bootstrap_days=60, replay_days=replay_days)
            result['seed'] = trade_seed(trade_id)
            if not isinstance(rule_version, str) or not rule_version:
                raise ValueError('immutable replay rule version is required')
            # Lazy providers execute inside the same error/deadline/alert boundary.
            # Cannot preempt a blocking provider, but never run cores after it overruns.
            if callable(bars):
                bars = bars()
            if time.perf_counter() >= deadline:
                raise TimeoutError('history provider budget exceeded')
            history = self._prepared(bars, symbol, decision_time, 60)
            result['input_hash'] = history.input_hash
            if variant != 'replay_only' and volatility_scale is None:
                estimator = current_volatility_scale(history)
                result['volatility_estimator'] = estimator
                volatility_scale = float(estimator['scale'])  # type: ignore[arg-type]
            elif volatility_scale is not None:
                result['volatility_estimator'] = {'estimator': 'explicit-fixed-scale', 'scale': volatility_scale}
            if time.perf_counter() >= deadline:
                raise TimeoutError('history preparation budget exceeded')
            replay: dict[str, object] = {'insufficient': True}
            if variant != 'simulation_only':
                replay_history = (history if replay_days == 60 else
                                  self._prepared(bars, symbol, decision_time, replay_days, minimum_days=60))
                if time.perf_counter() >= deadline:
                    raise TimeoutError('replay history preparation budget exceeded')
                result['replay_input_hash'] = replay_history.input_hash
                replay_key = (replay_history.input_hash, rule, rule_version, fee_rate, slippage_rate)
                cached_replay = self._replay_cache.get(replay_key)
                if cached_replay is None:
                    cached_replay = replay_rule(replay_history, rule, fee_rate=fee_rate, slippage_rate=slippage_rate,
                                                deadline=deadline)
                    self._replay_cache[replay_key] = cached_replay
                    if len(self._replay_cache) > self.cache_entries:
                        self._replay_cache.popitem(last=False)
                self._replay_cache.move_to_end(replay_key)
                replay = copy.deepcopy(cached_replay)
                result['replay'] = replay
            if variant != 'replay_only':
                simulation = simulate_trade(history, plan, trade_id, fee_rate=fee_rate,
                                            slippage_rate=slippage_rate, paths=paths,
                                            volatility_scale=volatility_scale if volatility_scale is not None else 1., deadline=deadline)
                result['simulation'] = simulation
                result.update(trade_verdict(simulation, replay, coin_flip_skip=coin_flip_skip))
            else:
                result.update(replay_only_verdict(replay, coin_flip_skip=coin_flip_skip))
                result['path_count'] = 0  # no bootstrap paths were run
            if time.perf_counter() >= deadline:
                # Never leave a partially computed permissive verdict on a timed-out result.
                result.update(verdict='SKIP', multiplier=0.)
                raise TimeoutError('check budget exceeded')
            self.overruns = 0
            if result['verdict'] == 'SKIP':
                result['alert_delivered'] = self._notify(f'Time Machine {trade_id}: SKIP: {result["reason"]}')
            return result
        except Exception as exc:  # noqa: BLE001 - every invalid/error outcome must skip
            timed_out = isinstance(exc, TimeoutError)
            self.overruns = self.overruns + 1 if timed_out else 0
            result.update(verdict='SKIP', multiplier=0., check_failed=True, reason=type(exc).__name__ + ': ' + str(exc), timed_out=timed_out)
            result['alert_delivered'] = self._notify(f'Time Machine {trade_id}: SKIP: {result["reason"]}')
            if self.overruns >= 3 and self.paths == 10000 and variant != 'replay_only':
                self.paths = 5000
                result['path_reduction_alert_delivered'] = self._notify(
                    'Time Machine: three consecutive overruns; subsequent checks reduced to 5000 paths')
            return result
        finally:
            result['elapsed_ms'] = (time.perf_counter() - started) * 1000
            result['next_path_count'] = self.paths
