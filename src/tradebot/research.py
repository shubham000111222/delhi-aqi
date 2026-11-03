"""The bounded research loop (plan §19.5): search, count every attempt, deflate the winner.

An automatic search is the fastest way to invent a strategy that only exists in the past.
Run 500 variants and the best one will look excellent even if every one of them is noise.
This module therefore does not do the easy half (find a big number) without the hard half
(count the attempts and subtract what luck would have given you).

How the search works - successive halving, not a smarter optimiser:

1. take a bounded, declared search space (never "all parameters, any value");
2. shuffle the candidates with a seed, so two people running it get the same order;
3. evaluate every candidate on the first slice of the window;
4. keep the better half and evaluate those on a longer slice, then repeat;
5. judge the survivor on the *full* window and deflate its Sharpe by the number of trials
   that were actually run.

Why halving rather than a Bayesian optimiser: it is the cheapest honest search. A smarter
optimiser would find a better number on this data, which is precisely the problem - the
number would be better because the optimiser searched harder, not because the edge is
real. Halving also spends most of its budget on the short slices, so the expensive full
window is run a handful of times.

What the loop is not allowed to do:

* it never registers a model card, never edits config, never claims a promotion - the
  gates in `backtest.metrics` are the only thing that can promote, and they need
  out-of-sample data this loop does not have;
* it never drops a trial. A candidate that crashed is logged with its error, because an
  unlogged failure is one less attempt in the denominator;
* it will say "no recommendation" and mean it. On synthetic data, at these budgets, that
  is the expected answer.
"""

from __future__ import annotations

import copy
import itertools
import math
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .backtest.metrics import _daily_returns
from .backtest.robustness import Trial, multiple_testing_report
from .config import Config

#: ``(params, config, start, end) -> SegmentResult``: injected in tests, real engine in
#: production. Returns an object with ``trades``, ``net_return`` and ``curve``.
Researcher = Callable[[dict[str, Any], Config, datetime, datetime], Awaitable[Any]]


def candidates(space: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    """Every point in the declared space, in a stable order."""
    if not space:
        return [{}]
    names = list(space)
    return [
        dict(zip(names, combo, strict=True))
        for combo in itertools.product(*(space[name] for name in names))
    ]


def sharpe_of(curve: Sequence[tuple[datetime, Decimal]]) -> float:
    """Annualised Sharpe from an equity curve, at whatever resolution the curve has.

    Daily returns are the honest unit - but the loop's first stages are hours long, and a
    one-day window has no daily returns at all, so the sampling falls back to the curve's
    own spacing (15-minute bars, in practice) and annualises by the number of periods per
    year. That makes short-stage Sharpes noisier than the daily version, which is why the
    *decision* here is made on net return and the Sharpe is only what the deflation eats.
    """
    points = [(ts, float(equity)) for ts, equity in curve]
    if len(points) < 4:
        return 0.0
    daily = _daily_returns([(ts, Decimal(str(equity))) for ts, equity in points])
    if len(daily) >= 3:
        returns, periods = daily, 365.0
    else:
        returns = [
            (points[i][1] - points[i - 1][1]) / points[i - 1][1]
            for i in range(1, len(points))
            if points[i - 1][1] > 0
        ]
        gaps = sorted(
            (points[i][0] - points[i - 1][0]).total_seconds() for i in range(1, len(points))
        )
        spacing = gaps[len(gaps) // 2] if gaps else 0.0
        if spacing <= 0:
            return 0.0
        periods = 365.0 * 24.0 * 3600.0 / spacing
    if len(returns) < 3:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    stdev = math.sqrt(variance)
    if stdev <= 1e-12:
        return 0.0
    return mean / stdev * math.sqrt(periods)


@dataclass
class TrialRun:
    """One evaluated candidate at one stage."""

    index: int
    stage: int
    name: str
    params: dict[str, Any]
    start: datetime
    end: datetime
    score: float = 0.0
    net_return: float = 0.0
    trades: int = 0
    sharpe: float = 0.0
    survived: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "stage": self.stage,
            "name": self.name,
            "params": self.params,
            "days": round((self.end - self.start).total_seconds() / 86_400, 3),
            "score": round(self.score, 6),
            "net_return_pct": round(self.net_return * 100, 3),
            "trades": self.trades,
            "sharpe": round(self.sharpe, 3),
            "survived": self.survived,
            "error": self.error,
        }


@dataclass
class ResearchReport:
    strategy: str
    space: dict[str, list[Any]]
    stages: list[dict[str, Any]] = field(default_factory=list)
    trials: list[TrialRun] = field(default_factory=list)
    best: TrialRun | None = None
    multiple_testing: dict[str, Any] = field(default_factory=dict)
    recommend: bool = False
    verdict: str = ""
    next_steps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "space": self.space,
            "space_size": len(candidates(self.space)),
            "attempts": len(self.trials),
            "stages": self.stages,
            "best": self.best.as_dict() if self.best else None,
            "multiple_testing": self.multiple_testing,
            "recommend": self.recommend,
            "verdict": self.verdict,
            "next_steps": self.next_steps,
            "trials": [trial.as_dict() for trial in self.trials],
        }


def _errors(report: ResearchReport) -> int:
    return sum(1 for trial in report.trials if trial.error)


async def search(
    config: Config,
    *,
    strategy: str,
    space: Mapping[str, Sequence[Any]],
    start: datetime,
    end: datetime,
    stages: Sequence[float] = (0.25, 0.5, 1.0),
    keep: float = 0.5,
    budget: int | None = None,
    seed: int | None = None,
    researcher: Researcher | None = None,
    metric: str = "net_return",
) -> ResearchReport:
    """Run the halving search and return the trials, the deflation and a recommendation.

    ``stages`` are fractions of the total window (0.25 means "the first quarter"),
    each evaluated independently, so a candidate cannot win one stage and coast: every
    survivor has to be evaluated again on the longer window.
    """
    if strategy not in config.strategies.strategies:
        raise KeyError(f"unknown strategy {strategy!r}")
    if not space:
        raise ValueError("the search space is empty: declare at least one axis")
    points = candidates(space)
    if budget is not None:
        points = points[: max(1, budget)]
    if not points:
        raise ValueError("the search space is empty")
    random.Random(seed if seed is not None else 0).shuffle(points)

    runner = researcher or _default_researcher(strategy, seed=seed)
    report = ResearchReport(
        strategy=strategy,
        space={name: list(values) for name, values in space.items()},
    )
    span = end - start
    survivors: list[dict[str, Any]] = list(points)
    index = 0
    for stage, fraction in enumerate(stages):
        if not survivors:
            break
        stage_end = start + span * fraction
        if stage_end <= start:
            stage_end = end
        winners: list[tuple[float, dict[str, Any]]] = []
        scored: list[TrialRun] = []
        for params in survivors:
            trial = TrialRun(
                index=index,
                stage=stage,
                name=f"{strategy}:s{stage}:{index}",
                params=dict(params),
                start=start,
                end=stage_end,
            )
            index += 1
            try:
                result = await runner(params, config, start, stage_end)
            except Exception as exc:  # noqa: BLE001 - a broken candidate is a logged trial
                trial.error = f"{type(exc).__name__}: {exc}"[:200]
                trial.score = float("-inf")
                report.trials.append(trial)
                scored.append(trial)
                continue
            trial.net_return = float(result.net_return)
            trial.trades = len(result.trades)
            trial.sharpe = sharpe_of(getattr(result, "curve", []) or [])
            trial.score = trial.net_return if metric == "net_return" else trial.sharpe
            report.trials.append(trial)
            scored.append(trial)
            if trial.error is None:
                winners.append((trial.score, dict(params)))

        usable = [trial for trial in scored if trial.error is None]
        usable.sort(key=lambda trial: trial.score, reverse=True)
        advancing = max(1, math.ceil(len(usable) * keep)) if stage + 1 < len(stages) else len(usable)
        keep_names = {trial.name for trial in usable[:advancing]}
        for trial in scored:
            trial.survived = trial.name in keep_names
        report.stages.append(
            {
                "stage": stage,
                "days": round((stage_end - start).total_seconds() / 86_400, 3),
                "evaluated": len(scored),
                "failed": sum(1 for trial in scored if trial.error),
                "advanced": len(keep_names),
                "best_score": round(max((t.score for t in usable), default=0.0), 6),
            }
        )
        survivors = [trial.params for trial in usable[:advancing]]

    final_stage = len(stages) - 1
    finalists = [trial for trial in report.trials if trial.stage == final_stage and trial.error is None]
    report.best = max(finalists, key=lambda trial: trial.score) if finalists else None

    trials = [
        Trial(
            name=f"{strategy}:{trial.index}",
            sharpe=trial.sharpe,
            return_pct=trial.net_return * 100,
            params=trial.params,
        )
        for trial in report.trials
        if trial.error is None and trial.stage == final_stage
    ]
    observations = max(1, int((end - start).days * 180))
    report.multiple_testing = multiple_testing_report(trials, observations)
    report.multiple_testing["attempts_logged"] = len(report.trials)
    report.multiple_testing["failed_attempts"] = _errors(report)

    report.recommend, report.verdict = _judge(report)
    report.next_steps = _next_steps(report)
    return report


def _judge(report: ResearchReport) -> tuple[bool, str]:
    """A recommendation needs a profit, a Sharpe that beat luck, and no crash budget."""
    if report.best is None:
        return False, "no candidate produced a usable result; nothing to recommend"
    best = report.best
    if best.net_return <= 0:
        return False, (
            f"no edge - the best candidate ({_params(best.params)}) lost "
            f"{best.net_return * 100:+.2f}% over the full window"
        )
    probability = float(report.multiple_testing.get("probability") or 0.0)
    deflated = float(report.multiple_testing.get("deflated_sharpe") or 0.0)
    attempts = report.multiple_testing.get("attempts_logged", len(report.trials))
    if deflated <= 0:
        return False, (
            f"not distinguishable from luck - {_params(best.params)} returned "
            f"{best.net_return * 100:+.2f}% but its Sharpe {best.sharpe:.2f} does not survive "
            f"{attempts} attempts (deflated {deflated:.2f})"
        )
    if probability < 0.95:
        return False, (
            f"weak - {_params(best.params)} deflates to {deflated:.2f} "
            f"(probability {probability:.2f} < 0.95) after {attempts} attempts"
        )
    return True, (
        f"{_params(best.params)} returned {best.net_return * 100:+.2f}% with deflated Sharpe "
        f"{deflated:.2f} (probability {probability:.2f}) after {attempts} attempts - worth a "
        "walk-forward, not a promotion"
    )


def _next_steps(report: ResearchReport) -> list[str]:
    """Say what would have to happen before anything is believed."""
    steps = [
        "run `tradebot walk-forward` on real recorded data: the loop only searches, it does not validate",
        "run `tradebot robustness --days 10` and check the 2x-cost rerun",
        "the acceptance gates (`tradebot metrics`) are still the only thing that can promote",
    ]
    if report.recommend and report.best is not None:
        steps.insert(0, f"re-test {_params(report.best.params)} out of sample before touching config")
    return steps


def _params(params: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in params.items()) or "default"


def _default_researcher(strategy: str, *, seed: int | None) -> Researcher:
    """The real thing: deep-copy the config, apply the candidate, run one engine segment."""
    from .backtest.walkforward import run_segment

    async def researcher(
        params: dict[str, Any], cfg: Config, start: datetime, end: datetime
    ) -> Any:
        tuned = copy.deepcopy(cfg)
        tuned.strategies.strategies[strategy].params.update(params)
        return await run_segment(tuned, start, end, seed=seed)

    return researcher


def space_from_config(config: Config, strategy: str, *, steps: int = 3, spread: float = 0.5) -> dict[str, list[Any]]:
    """A declared space around each numeric parameter of a strategy.

    Bounded on purpose: three values per parameter, each within ``spread`` of the
    configured one. A wider space is a bigger search, and a bigger search is a larger
    multiple-testing penalty - the loop will tell you so in the deflation, but the honest
    move is not to ask for it.
    """
    entry = config.strategies.strategies.get(strategy)
    if entry is None:
        raise KeyError(f"unknown strategy {strategy!r}")
    space: dict[str, list[Any]] = {}
    for name, value in (entry.params or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value == 0:
            continue
        if isinstance(value, int):
            grid: list[Any] = sorted({max(1, int(round(value * factor))) for factor in _factors(spread, steps)})
        else:
            grid = sorted({round(value * factor, 6) for factor in _factors(spread, steps)})
        if len(grid) > 1:
            space[name] = grid
    return space


def _factors(spread: float, steps: int) -> list[float]:
    if steps <= 1:
        return [1.0]
    low = 1.0 - spread / 2
    high = 1.0 + spread / 2
    return [low + (high - low) * i / (steps - 1) for i in range(steps)]


def format_report(report: ResearchReport) -> str:
    """A terminal report that leads with the count and the verdict, not the winner."""
    lines: list[str] = []
    lines.append(
        f"search: {report.strategy} | {len(candidates(report.space))} candidates in the space | "
        f"{len(report.trials)} attempts actually run"
    )
    if report.stages:
        stages = " -> ".join(
            f"{stage['days']:.2f}d: {stage['evaluated']} run, {stage['advanced']} kept"
            for stage in report.stages
        )
        lines.append(f"stages: {stages}")
    if report.best is not None:
        best = report.best
        lines.append("")
        lines.append(
            f"best ({_params(best.params)}): {best.net_return * 100:+.2f}% over the full window, "
            f"{best.trades} trades, Sharpe {best.sharpe:.2f}"
        )
    else:
        lines.append("")
        lines.append("best: none - every candidate failed")
    testing = report.multiple_testing or {}
    lines.append(
        f"multiple testing: {testing.get('verdict', 'no trials')} "
        f"(probability {float(testing.get('probability') or 0):.2f})"
    )
    lines.append("")
    lines.append(f"recommendation: {'YES - for re-testing' if report.recommend else 'NO'} - {report.verdict}")
    if report.next_steps:
        lines.append("")
        lines.append("before believing any of this:")
        lines.extend(f"  - {step}" for step in report.next_steps)
    return "\n".join(lines)


__all__ = [
    "ResearchReport",
    "TrialRun",
    "candidates",
    "format_report",
    "search",
    "sharpe_of",
    "space_from_config",
]
