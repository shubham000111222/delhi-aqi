"""Walk-forward validation: rolling train/test windows, purged CV, stitched results.

A single train/test split answers one question on one slice of history. Walk-forward
asks the question repeatedly: pick parameters on the past, trade the future, roll
forward, and see whether the *sequence* of out-of-sample results survives. A strategy
whose OOS windows are mostly negative is not a strategy, however good one window was.

Everything here is pure bookkeeping around the engine: each window is an independent
`Engine` run with its own store (so no state leaks across the boundary), and the only
thing carried forward is capital. That mirrors what live trading would have looked
like — you cannot carry a position through a retrain.
"""

from __future__ import annotations

import copy
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..config import Config, Mode
from ..core.money import D, to_str
from .metrics import (
    TradeRecord,
    acceptance_gates,
    all_gates_passed,
    compute_metrics,
    monte_carlo_drawdown,
)
from .runner import collect


@dataclass(frozen=True)
class Window:
    """One train/test pair. The test window is strictly after the train window."""

    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime

    @property
    def test_seconds(self) -> float:
        return (self.test_end - self.test_start).total_seconds()

    @property
    def label(self) -> str:
        return f"W{self.index} {self.test_start:%Y-%m-%d}..{self.test_end:%Y-%m-%d}"

    def overlaps(self, other: Window) -> bool:
        return self.test_start < other.test_end and other.test_start < self.test_end


def rolling_windows(
    start: datetime,
    end: datetime,
    *,
    train_days: float = 180,
    test_days: float = 30,
    step_days: float | None = None,
) -> list[Window]:
    """Roll a fixed-size train/test pair across ``start``..``end``.

    ``step_days`` defaults to ``test_days`` (contiguous, non-overlapping tests). A
    smaller step produces overlapping tests, which is useful for a dense stability
    picture but double-counts the overlapping days, so the default is the honest one.
    """
    if train_days <= 0 or test_days <= 0:
        raise ValueError("train_days and test_days must be positive")
    step = float(step_days if step_days is not None else test_days)
    if step <= 0:
        raise ValueError("step_days must be positive")
    if end <= start:
        raise ValueError("end must be after start")

    windows: list[Window] = []
    test_start = utc_plus(start, train_days)
    index = 0
    while test_start + timedelta(days=test_days) <= end:
        train_start = test_start - timedelta(days=train_days)
        windows.append(
            Window(
                index=index,
                train_start=train_start,
                train_end=test_start,
                test_start=test_start,
                test_end=test_start + timedelta(days=test_days),
            )
        )
        test_start = test_start + timedelta(days=step)
        index += 1
    return windows


def utc_plus(start: datetime, days: float) -> datetime:
    return start + timedelta(days=days)


def purged_folds(
    event_times: Sequence[datetime],
    *,
    n_folds: int = 5,
    horizon: timedelta = timedelta(hours=24),
    embargo: timedelta = timedelta(hours=1),
) -> list[tuple[list[int], list[int]]]:
    """Purged, embargoed K-fold splits (Lopez de Prado) over time-ordered samples.

    A label at ``t`` looks ``horizon`` into the future, so any training sample whose
    label window touches the test block leaks. Purge removes those samples; the embargo
    additionally drops samples immediately *after* the test block, because consecutive
    bars share information through their indicators. Without both, a CV score on
    financial series is optimistic — often wildly so.

    Returns ``(train_index, test_index)`` pairs. Folds are contiguous blocks of time.
    """
    if n_folds < 2:
        raise ValueError("n_folds must be at least 2")
    if not event_times:
        return []
    order = sorted(range(len(event_times)), key=lambda i: event_times[i])
    times = [event_times[i] for i in order]
    block = max(1, len(times) // n_folds)

    folds: list[tuple[list[int], list[int]]] = []
    for fold in range(n_folds):
        lo = fold * block
        hi = len(times) if fold == n_folds - 1 else min((fold + 1) * block, len(times))
        if lo >= hi:
            continue
        test_start, test_end = times[lo], times[hi - 1]
        train: list[int] = []
        test: list[int] = []
        for pos, ts in enumerate(times):
            if lo <= pos < hi:
                test.append(order[pos])
            elif ts + horizon < test_start:
                # label window ends before the test block begins: safe
                train.append(order[pos])
            elif ts > test_end + embargo:
                # far enough after the test block: safe
                train.append(order[pos])
        folds.append((train, test))
    return folds


@dataclass
class SegmentResult:
    """One engine run over a slice of time."""

    start_equity: Decimal
    end_equity: Decimal
    trades: list[TradeRecord] = field(default_factory=list)
    curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def net_return(self) -> Decimal:
        if self.start_equity <= 0:
            return D("0")
        return (self.end_equity - self.start_equity) / self.start_equity

    def as_row(self) -> dict[str, Any]:
        return {
            "start_equity": to_str(self.start_equity, 2),
            "end_equity": to_str(self.end_equity, 2),
            "net_return": to_str(self.net_return, 4),
            "trades": len(self.trades),
            "fees": to_str(sum((t.fees for t in self.trades), D("0")), 2),
        }


async def run_segment(
    config: Config,
    start: datetime,
    end: datetime,
    *,
    capital: Decimal | None = None,
    seed: int | None = None,
) -> SegmentResult:
    """Run the real engine from ``start`` to ``end`` with fresh state."""
    from ..core.clock import SimClock
    from ..core.money import to_str as _to_str
    from ..core.store import Store
    from ..engine import Engine

    if config.mode is not Mode.PAPER:
        raise ValueError("backtest segments require paper mode")
    cfg = copy.deepcopy(config)
    clock = SimClock(start)
    store = Store(":memory:")
    engine = await Engine.build(cfg, clock=clock, store=store)
    if capital is not None:
        engine.ledger.starting_cash = capital
        engine.ledger.cash = capital
        engine.adapter.cash = capital  # type: ignore[attr-defined]  # paper adapter exposes cash
        engine.starting_capital = capital
        store.kv_set("day_start_equity", _to_str(capital))
        store.kv_set("week_start_equity", _to_str(capital))
        store.kv_set("peak_equity", _to_str(capital))
    if seed is not None and hasattr(engine.feed, "rng"):
        engine.feed.rng.seed(seed)

    await engine.run(stop_at=end)
    curve, trades, _ = collect(store)
    result = SegmentResult(
        start_equity=engine.starting_capital,
        end_equity=engine.ledger.equity(engine.marks),
        trades=trades,
        curve=curve,
        stats=engine.stats.to_dict(),
    )
    engine.close()
    return result


#: A runner is injected so tests can exercise the window logic without trading.
SegmentRunner = Callable[..., Any]


@dataclass
class WindowOutcome:
    window: Window
    test: SegmentResult
    params: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        row = {"window": self.window.label, **self.test.as_row()}
        if self.params:
            row["params"] = self.params
        return row


def summarise(outcomes: Sequence[WindowOutcome], starting_capital: Decimal) -> dict[str, Any]:
    """Aggregate OOS outcomes: is the *sequence* of windows convincing?"""
    if not outcomes:
        return {
            "windows": 0,
            "profitable_windows": 0,
            "profitable_share": 0.0,
            "median_return": 0.0,
            "compound_return": 0.0,
            "return_dispersion": 0.0,
            "verdict": "no windows",
        }
    returns = [float(o.test.net_return) for o in outcomes]
    profitable = sum(1 for r in returns if r > 0)
    compound = 1.0
    for r in returns:
        compound *= 1.0 + r
    dispersion = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    median = statistics.median(returns)
    share = profitable / len(returns)

    if share >= 0.6 and compound > 1.0:
        verdict = "consistent"
    elif share >= 0.4 and compound > 1.0:
        verdict = "mixed"
    else:
        verdict = "inconsistent"
    return {
        "windows": len(outcomes),
        "profitable_windows": profitable,
        "profitable_share": round(share, 3),
        "median_return": round(median, 4),
        "compound_return": round(compound - 1.0, 4),
        "return_dispersion": round(dispersion, 4),
        "verdict": verdict,
        "starting_capital": to_str(starting_capital, 2),
    }


def stitched_curve(outcomes: Sequence[WindowOutcome]) -> list[tuple[datetime, Decimal]]:
    """Concatenate per-window equity curves for one continuous picture."""
    # Each window starts at the previous window's ending equity, so the curve is
    # continuous in money even though the state itself does not cross the boundary.
    return [point for outcome in outcomes for point in outcome.test.curve]


def pooled_trades(outcomes: Sequence[WindowOutcome]) -> list[TradeRecord]:
    return [trade for outcome in outcomes for trade in outcome.test.trades]


async def run_walk_forward(
    config: Config,
    windows: Sequence[Window],
    *,
    capital: Decimal | None = None,
    seed: int | None = None,
    runner: SegmentRunner = run_segment,
    train_fn: Callable[[Window, dict[str, Any]], dict[str, Any]] | None = None,
    cost_stress: bool = False,
) -> dict[str, Any]:
    """Run every window: fit on ``train``, trade ``test``, carry the capital forward.

    ``train_fn`` receives the window and the training evaluation and may return config
    overrides (for example parameters chosen in-sample). The default keeps the configured
    parameters, which still produces a valid out-of-sample sequence — only the *choice* of
    parameters is not re-fitted, and the report says so.
    """
    if config.mode is not Mode.PAPER:
        raise ValueError("walk-forward requires paper mode")
    starting = capital if capital is not None else D(str(config.settings.starting_capital_usdt))
    if not starting.is_finite() or starting <= 0:
        raise ValueError("walk-forward capital must be finite and positive")
    running = starting
    outcomes: list[WindowOutcome] = []
    overrides: dict[str, Any] = {}

    for window in windows:
        cfg = copy.deepcopy(config)
        _apply_overrides(cfg, overrides)

        if train_fn is not None:
            train = await runner(cfg, window.train_start, window.train_end, capital=running, seed=seed)
            overrides = train_fn(window, train.as_row()) or {}
            cfg = copy.deepcopy(config)
            _apply_overrides(cfg, overrides)

        test = await runner(cfg, window.test_start, window.test_end, capital=running, seed=seed)
        running = test.end_equity
        outcomes.append(WindowOutcome(window=window, test=test, params=dict(overrides)))

    summary = summarise(outcomes, starting)
    summary["parameters_refit"] = train_fn is not None

    trades = pooled_trades(outcomes)
    curve = stitched_curve(outcomes)
    metrics = compute_metrics(
        curve,
        trades,
        starting_equity=starting,
        ending_equity=running,
        tax_rate=config.risk.tax_reserve_pct,
    )
    mc = monte_carlo_drawdown([float(t.pnl) for t in trades], float(starting))
    stress_result: dict[str, Any] | None = None
    stress_metrics = None
    if cost_stress and outcomes:
        from .robustness import stress_config

        stressed_config = stress_config(config, 2.0)
        stressed_running = starting
        stressed_outcomes: list[WindowOutcome] = []
        for outcome in outcomes:
            cfg = copy.deepcopy(stressed_config)
            # Use the exact parameters selected for the corresponding nominal window;
            # do not refit on the stressed test data or reuse nominal ending capital.
            _apply_overrides(cfg, outcome.params)
            test = await runner(cfg, outcome.window.test_start, outcome.window.test_end,
                                capital=stressed_running, seed=seed)
            stressed_running = test.end_equity
            stressed_outcomes.append(WindowOutcome(window=outcome.window, test=test, params=dict(outcome.params)))
        stress_metrics = compute_metrics(
            stitched_curve(stressed_outcomes), pooled_trades(stressed_outcomes), starting_equity=starting,
            ending_equity=stressed_running, tax_rate=config.risk.tax_reserve_pct,
        )
        stress_result = {
            "multiplier": 2, "windows": [o.as_row() for o in stressed_outcomes],
            "metrics": stress_metrics.to_dict(), "ending_equity": str(stressed_running),
            "model": "2x fees and adverse modeled market slippage; limit fills remain at limit",
            "runner_verified": runner is run_segment,
            "venue_costs_verified": False,
        }
    # Injected runners are useful for tests but cannot constitute rerun evidence.
    gates = acceptance_gates(metrics, mc=mc,
                             cost_stress_metrics=stress_metrics if runner is run_segment else None)

    return {
        "windows": [o.as_row() for o in outcomes],
        "summary": summary,
        "metrics": metrics,
        "metrics_dict": metrics.to_dict(),
        "monte_carlo": mc,
        "cost_stress": stress_result,
        "gates": gates,
        "passed": all_gates_passed(gates) and summary["verdict"] == "consistent",
        "outcomes": outcomes,
    }


def _apply_overrides(config: Config, overrides: dict[str, Any]) -> None:
    """Apply ``{"strategies": {"S1_ema_trend": {"ema_fast": 12}}}`` or a flat param dict."""
    strategies = overrides.get("strategies", overrides)
    if not isinstance(strategies, dict):
        return
    for name, params in strategies.items():
        entry = config.strategies.strategies.get(name)
        if entry is None or not isinstance(params, dict):
            continue
        for key, value in params.items():
            if key == "enabled":
                entry.enabled = bool(value)
            else:
                entry.params[key] = value


def format_walk_forward(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = ["walk-forward (out-of-sample test windows only)"]
    lines.append(f"{'window':22s} {'trades':>7s} {'return':>9s} {'end equity':>12s}")
    for row in result["windows"]:
        lines.append(
            f"{row['window']:22s} {row['trades']:7d} {float(row['net_return']) * 100:+8.2f}% "
            f"{float(row['end_equity']):12.2f}"
        )
    lines.append("")
    lines.append(
        f"windows {summary['windows']} | profitable {summary['profitable_windows']} "
        f"({summary['profitable_share']:.0%}) | median {summary['median_return'] * 100:+.2f}% "
        f"| compound {summary['compound_return'] * 100:+.2f}% "
        f"| dispersion {summary['return_dispersion'] * 100:.2f}% | {summary['verdict']}"
    )
    stress = result.get("cost_stress")
    if stress is not None:
        lines.append(f"2x modeled cost rerun: return {stress['metrics']['total_return'] * 100:+.2f}% "
                     f"| ending equity {stress['ending_equity']} | venue cost unverified")
    if not summary.get("parameters_refit", True):
        lines.append("note: parameters were held fixed (no in-sample re-fit)")
    return "\n".join(lines)
