"""Robustness suite (plan §8): the checks that decide whether a result is real.

A single backtest number is an anecdote. This module asks the questions that make it
evidence:

* **Is the result path-dependent?** — resample the *returns* in blocks (block bootstrap)
  and the *trades* in random order, and look at the whole distribution of drawdown and
  return, not the one path that happened.
* **Does it survive worse costs?** — rerun the whole backtest with fees and slippage
  doubled, rather than assuming the edge is thick enough. Post-hoc fee subtraction is
  offered too, but the rerun is the honest one: a 2x cost world also changes which
  orders fill and which stops trigger.
* **Did we get lucky among many attempts?** — every variant tried is logged, and the best
  Sharpe is deflated against the number of trials (Bailey & López de Prado).
* **Did we beat doing nothing?** — the strategy is compared with buy-and-hold of the same
  instruments over the same window, because a strategy that "returns 3%" in a market that
  rose 20% has not earned its fees.

Nothing here can turn a failure into a pass. Every function returns numbers and a plain
verdict.
"""

from __future__ import annotations

import copy
import math
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..core.money import D
from .metrics import TradeRecord, _daily_returns, deflated_sharpe, psr_probability, return_moments

# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def block_bootstrap(
    series: Sequence[float],
    *,
    iterations: int = 2000,
    block: int = 5,
    seed: int = 7,
) -> dict[str, float]:
    """Resample ``series`` in contiguous blocks.

    Blocks, not single points: daily returns and P&L are autocorrelated, and an
    i.i.d. bootstrap would invent a smoother, friendlier distribution than reality.
    """
    values = [float(v) for v in series]
    if not values:
        return {"p05": 0.0, "p50": 0.0, "p95": 0.0, "mean": 0.0, "stdev": 0.0, "negative_share": 0.0}
    size = max(1, min(block, len(values)))
    rng = random.Random(seed)
    totals: list[float] = []
    for _ in range(iterations):
        sample: list[float] = []
        while len(sample) < len(values):
            start = rng.randrange(0, len(values) - size + 1)
            sample.extend(values[start : start + size])
        totals.append(sum(sample[: len(values)]))
    totals.sort()

    def q(pct: float) -> float:
        return totals[min(len(totals) - 1, max(0, int(round(pct * (len(totals) - 1)))))]

    return {
        "p05": q(0.05),
        "p50": q(0.50),
        "p95": q(0.95),
        "mean": statistics.fmean(totals),
        "stdev": statistics.pstdev(totals) if len(totals) > 1 else 0.0,
        "negative_share": sum(1 for t in totals if t < 0) / len(totals),
    }


def bootstrap_drawdown(
    daily_returns: Sequence[float],
    *,
    iterations: int = 2000,
    block: int = 5,
    seed: int = 7,
) -> dict[str, float]:
    """Block-bootstrap the *return path*, then measure drawdown on each path."""
    values = [float(v) for v in daily_returns]
    if not values:
        return {"p50_drawdown": 0.0, "p95_drawdown": 0.0, "median_total": 0.0}
    size = max(1, min(block, len(values)))
    rng = random.Random(seed)
    drawdowns: list[float] = []
    totals: list[float] = []
    for _ in range(iterations):
        path: list[float] = []
        while len(path) < len(values):
            start = rng.randrange(0, len(values) - size + 1)
            path.extend(values[start : start + size])
        path = path[: len(values)]
        equity, peak, worst = 1.0, 1.0, 0.0
        for ret in path:
            equity *= 1.0 + ret
            peak = max(peak, equity)
            worst = min(worst, equity / peak - 1.0)
        drawdowns.append(worst)
        totals.append(equity - 1.0)
    drawdowns.sort()
    totals.sort()

    def q(seq: list[float], pct: float) -> float:
        return seq[min(len(seq) - 1, max(0, int(round(pct * (len(seq) - 1)))))]

    return {
        "p50_drawdown": q(drawdowns, 0.50),
        "p95_drawdown": q(drawdowns, 0.05),
        "median_total": q(totals, 0.50),
    }


# ---------------------------------------------------------------------------
# cost stress
# ---------------------------------------------------------------------------


def stressed_trade_pnls(trades: Sequence[TradeRecord], multiplier: float) -> list[float]:
    """Approximate 2x costs by charging the extra fees to each round trip.

    Fast, but it cannot know about the trades that the worse world would never have
    taken. Use it for a quick read; `run_cost_stress` is the answer that counts.
    """
    extra = max(0.0, multiplier - 1.0)
    return [float(t.pnl) - abs(float(t.fees)) * extra for t in trades]


def curves_after_stress(
    curve: Sequence[tuple[datetime, Decimal]],
    stress: float,
    *,
    starting_equity: Decimal,
) -> list[tuple[datetime, Decimal]]:
    """Rebuild an equity curve after subtracting ``stress`` from the final equity."""
    if not curve:
        return []
    out = [(ts, eq) for ts, eq in curve]
    last_ts, last_eq = out[-1]
    out[-1] = (last_ts, last_eq - D(str(stress)))
    del starting_equity  # kept for signature symmetry; the curve already includes it
    return out


def stress_config(config: Any, multiplier: float) -> Any:
    """Scale simulated fees and adverse market execution slippage; never mutate the source.

    This is a modelled stress, not verified real venue cost. Limit fills remain at their
    limit price and no missing slippage is invented where the model has no adverse cost.
    """
    factor = D(str(multiplier))
    if not factor.is_finite() or factor < 1 or factor > 10:
        raise ValueError("cost multiplier must be finite and between 1 and 10")
    stressed = copy.deepcopy(config)
    paper = stressed.settings.paper
    paper.maker_fee = paper.maker_fee * factor
    paper.taker_fee = paper.taker_fee * factor
    paper.slippage_multiplier = paper.slippage_multiplier * factor
    if not paper.slippage_multiplier.is_finite() or paper.slippage_multiplier > 10:
        raise ValueError("resulting slippage multiplier exceeds model limit")
    live = stressed.settings.live_fees
    live.maker = live.maker * factor
    live.taker = live.taker * factor
    return stressed


# ---------------------------------------------------------------------------
# multiple testing
# ---------------------------------------------------------------------------


@dataclass
class Trial:
    """One thing that was tried. Logged whether it worked or not."""

    name: str
    sharpe: float
    return_pct: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sharpe": self.sharpe,
            "return_pct": self.return_pct,
            "params": self.params,
            "at": self.at,
        }


def expected_max_sharpe(trials: int) -> float:
    """The Sharpe you would expect to see by luck alone after ``trials`` attempts.

    Bailey & López de Prado: with N independent trials of a zero-edge strategy, the best
    observed Sharpe is approximately sqrt(2 ln N) in annualised units.
    """
    if trials <= 1:
        return 0.0
    return math.sqrt(2.0 * math.log(trials))


def multiple_testing_report(
    trials: Sequence[Trial],
    observations: int,
    *,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> dict[str, Any]:
    """Deflate the best trial for the number of attempts that produced it.

    ``skew``/``kurtosis`` come from the same return series as the Sharpe; passing them
    applies the non-normality correction. With the defaults the correction is neutral.
    """
    if not trials:
        return {
            "trials": 0,
            "best": None,
            "expected_max_sharpe_by_luck": 0.0,
            "deflated_sharpe": 0.0,
            "verdict": "no trials logged - nothing to deflate",
        }
    best = max(trials, key=lambda t: t.sharpe)
    luck = expected_max_sharpe(len(trials))
    deflated = deflated_sharpe(
        best.sharpe,
        trials=len(trials),
        observations=max(observations, 1),
        skew=skew,
        kurtosis=kurtosis,
    )
    if best.sharpe <= 0:
        verdict = "no edge - the best trial lost money"
    elif best.sharpe <= luck:
        verdict = (
            f"not distinguishable from luck - best Sharpe {best.sharpe:.2f} is no better than "
            f"the {luck:.2f} expected from {len(trials)} attempts"
        )
    elif deflated <= 0:
        verdict = f"deflated Sharpe {deflated:.2f} <= 0 after {len(trials)} trials"
    else:
        verdict = f"survives multiple-testing correction: deflated Sharpe {deflated:.2f}"
    return {
        "trials": len(trials),
        "best": best.as_dict(),
        "expected_max_sharpe_by_luck": luck,
        "deflated_sharpe": deflated,
        "probability": round(psr_probability(deflated), 4),
        "skew": round(skew, 3),
        "kurtosis": round(kurtosis, 3),
        "verdict": verdict,
    }


def trial_log_from_sensitivity(cells: Sequence[dict[str, Any]], *, prefix: str = "") -> list[Trial]:
    """Turn sensitivity-grid cells into trials, so every grid point counts as an attempt."""
    trials: list[Trial] = []
    for index, cell in enumerate(cells):
        score = cell.get("score")
        if score is None:
            continue
        params = {k: v for k, v in cell.items() if k not in ("score", "metrics")}
        trials.append(
            Trial(
                name=f"{prefix}cell{index}",
                sharpe=float(score),
                return_pct=float(cell.get("return_pct", 0.0) or 0.0),
                params=params,
            )
        )
    return trials


def log_trials(
    store: Any,
    strategy: str,
    cells: Sequence[dict[str, Any]],
    *,
    metric: str,
    seed: int | None = None,
    run_prefix: str = "trial",
) -> int:
    """Persist every grid cell as an experiment row: the multiple-testing log.

    A trial that is not written down cannot be counted later, and an uncounted trial is
    how a search of 500 variants gets reported as one happy backtest. Rows are keyed by
    a deterministic run id, so re-running the same grid does not inflate the count.
    """
    logged = 0
    for index, cell in enumerate(cells):
        if cell.get("score") is None:
            continue
        params = {k: v for k, v in cell.items() if k not in ("score", "trades", "end_equity")}
        run_id = f"{run_prefix}:{strategy}:{metric}:{index}"
        store.save_experiment(
            run_id,
            kind="sensitivity_cell",
            name=f"{strategy} cell {index}",
            strategy=strategy,
            params=params,
            seed=seed,
            metrics={"score": cell.get("score"), "trades": cell.get("trades"), "metric": metric},
            notes="parameter grid search",
        )
        logged += 1
    return logged


def trials_from_store(store: Any, strategy: str, *, limit: int = 1000) -> list[Trial]:
    """Rebuild the trial list from the experiment log."""
    trials: list[Trial] = []
    for row in store.experiments(limit=limit, strategy=strategy):
        if row.get("kind") != "sensitivity_cell":
            continue
        metrics = row.get("metrics") or {}
        score = metrics.get("score")
        if score is None:
            continue
        trials.append(
            Trial(
                name=str(row.get("name") or row.get("run_id")),
                sharpe=float(score),
                params=dict(row.get("params") or {}),
                at=str(row.get("created_at") or ""),
            )
        )
    return trials


# ---------------------------------------------------------------------------
# benchmark
# ---------------------------------------------------------------------------


def buy_and_hold(
    closes: Mapping[str, Sequence[float]],
    *,
    starting_equity: float = 1000.0,
    fee_rate: float = 0.001,
) -> dict[str, Any]:
    """Equal-weight buy-and-hold over the same window, fees included.

    This is the number the strategy has to beat to have earned anything: passive exposure
    to the same instruments for the same time.
    """
    per_symbol: dict[str, float] = {}
    for symbol, prices in closes.items():
        values = [float(p) for p in prices if p]
        if len(values) < 2 or values[0] <= 0:
            continue
        gross = values[-1] / values[0]
        per_symbol[symbol] = (gross - 1.0) - 2.0 * fee_rate  # in and out
    if not per_symbol:
        return {"return_pct": 0.0, "per_symbol": {}, "equity": starting_equity}
    average = statistics.fmean(per_symbol.values())
    return {
        "return_pct": average * 100.0,
        "per_symbol": {k: round(v * 100.0, 4) for k, v in per_symbol.items()},
        "equity": starting_equity * (1.0 + average),
    }


def excess_over_benchmark(strategy_return_pct: float, benchmark_return_pct: float) -> dict[str, Any]:
    excess = strategy_return_pct - benchmark_return_pct
    if benchmark_return_pct == 0:
        verdict = "no benchmark available"
    elif excess > 0:
        verdict = f"beat buy-and-hold by {excess:+.2f} points"
    else:
        verdict = f"trailed buy-and-hold by {excess:+.2f} points - fees and work with less return"
    return {"excess_pct": excess, "verdict": verdict}


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------


def attribution(trades: Sequence[TradeRecord], *, by_hour: bool = True) -> dict[str, Any]:
    """Where the money actually came from: by strategy, symbol, exit reason and hour."""
    def bucket(key: Any) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for trade in trades:
            name = str(key(trade))
            entry = out.setdefault(name, {"pnl": 0.0, "trades": 0, "wins": 0, "fees": 0.0})
            entry["pnl"] += float(trade.pnl)
            entry["fees"] += float(trade.fees)
            entry["trades"] += 1
            entry["wins"] += 1 if trade.pnl > 0 else 0
        for entry in out.values():
            entry["win_rate"] = entry["wins"] / entry["trades"] if entry["trades"] else 0.0
            entry["pnl"] = round(entry["pnl"], 4)
            entry["fees"] = round(entry["fees"], 4)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["pnl"]))

    gross = sum(float(t.pnl) for t in trades)
    fees = sum(float(t.fees) for t in trades)
    report: dict[str, Any] = {
        "by_strategy": bucket(lambda t: t.strategy),
        "by_symbol": bucket(lambda t: t.symbol),
        "by_exit_reason": bucket(lambda t: t.reason or "unknown"),
        "net_pnl": round(gross, 4),
        "fees": round(fees, 4),
        "fee_drag_pct_of_gross": round(fees / abs(gross) * 100.0, 2) if gross else 0.0,
        "trades": len(trades),
    }
    if by_hour:
        report["by_hour"] = bucket(lambda t: t.closed_at.hour if t.closed_at else -1)
    if trades:
        best = max(trades, key=lambda t: t.pnl)
        worst = min(trades, key=lambda t: t.pnl)
        report["best_trade"] = {"symbol": best.symbol, "pnl": float(best.pnl)}
        report["worst_trade"] = {"symbol": worst.symbol, "pnl": float(worst.pnl)}
        # what happens if the single best trade were luck: remove it and see if profit survives
        without_best = gross - float(best.pnl)
        report["pnl_without_best_trade"] = round(without_best, 4)
        report["depends_on_one_trade"] = without_best <= 0 < gross
    return report


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def daily_returns_from_curve(curve: Sequence[tuple[datetime, Decimal]]) -> list[float]:
    return _daily_returns(list(curve))


def robustness_report(
    *,
    curve: Sequence[tuple[datetime, Decimal]],
    trades: Sequence[TradeRecord],
    starting_equity: Decimal,
    cost_stress_metrics: Any | None = None,
    cost_multiplier: float = 2.0,
    benchmark: dict[str, Any] | None = None,
    closes: Mapping[str, Sequence[float]] | None = None,
    iterations: int = 2000,
    seed: int = 7,
    trials: Sequence[Trial] | None = None,
) -> dict[str, Any]:
    """Everything the gates need, in one dict, with plain-language verdicts."""
    returns = daily_returns_from_curve(curve)
    start = float(starting_equity)
    end = float(curve[-1][1]) if curve else start
    strategy_return_pct = (end - start) / start * 100.0 if start else 0.0

    trade_pnls = [float(t.pnl) for t in trades]
    from .metrics import monte_carlo_drawdown

    trade_mc = monte_carlo_drawdown(trade_pnls, start, iterations=iterations, seed=seed)
    returns_mc = bootstrap_drawdown(returns, iterations=iterations, seed=seed)
    stressed = stressed_trade_pnls(trades, cost_multiplier)

    checks: dict[str, dict[str, Any]] = {}
    checks["trade_order_bootstrap"] = {
        "p95_drawdown": trade_mc["p95_drawdown"],
        "p50_return": trade_mc["p50_return"],
        "ok": abs(trade_mc["p95_drawdown"]) <= 0.20,
        "detail": f"MC p95 DD {trade_mc['p95_drawdown'] * 100:.2f}% (limit 20%)",
    }
    checks["return_block_bootstrap"] = {
        "p95_drawdown": returns_mc["p95_drawdown"],
        "median_return": returns_mc["median_total"],
        "ok": abs(returns_mc["p95_drawdown"]) <= 0.20,
        "detail": f"block bootstrap p95 DD {returns_mc['p95_drawdown'] * 100:.2f}% (limit 20%)",
    }
    stressed_total = sum(stressed)
    checks["cost_stress"] = {
        "multiplier": cost_multiplier,
        "pnl_at_stress": round(stressed_total, 4),
        "ok": stressed_total > 0,
        "detail": (
            f"P&L at {cost_multiplier:g}x fees {stressed_total:+.2f}: "
            + ("still positive" if stressed_total > 0 else "turns negative - the edge is the fee")
        ),
    }
    if cost_stress_metrics is not None:
        rerun_return = float(getattr(cost_stress_metrics, "total_return", 0.0))
        erosion = strategy_return_pct - rerun_return * 100.0
        thin = rerun_return > 0 and (
            erosion >= 0.5 or (strategy_return_pct > 0 and rerun_return * 100.0 < 0.25 * strategy_return_pct)
        )
        checks["cost_stress_rerun"] = {
            "return_pct": round(rerun_return * 100.0, 4),
            "erosion_points": round(erosion, 4),
            "ok": rerun_return > 0,
            "thin_margin": thin,
            "detail": (
                f"full rerun at {cost_multiplier:g}x costs returned {rerun_return * 100:+.2f}% "
                f"({erosion:+.2f} points of the nominal result)"
                + (" - survives, but the margin is thin" if thin else "")
            ),
        }

    out: dict[str, Any] = {
        "strategy_return_pct": round(strategy_return_pct, 4),
        "observations": len(returns),
        "trade_order_bootstrap": trade_mc,
        "return_block_bootstrap": returns_mc,
        "stressed_pnl": round(stressed_total, 4),
        "checks": checks,
    }
    if benchmark is not None or closes:
        bench = benchmark or buy_and_hold(closes or {})
        out["benchmark"] = bench
        out["benchmark_comparison"] = excess_over_benchmark(strategy_return_pct, bench["return_pct"])
    out["attribution"] = attribution(trades)
    if trials:
        skew, kurtosis = return_moments(returns)
        out["multiple_testing"] = multiple_testing_report(
            trials, observations=max(len(returns), 1), skew=skew, kurtosis=kurtosis
        )
    out["verdict"] = _verdict(checks)
    return out


def _verdict(checks: dict[str, dict[str, Any]]) -> str:
    """A plain answer to "would this survive contact with reality?" - not a promotion.

    Promotion needs the OOS gates, which live in `metrics.acceptance_gates`. Passing here
    on a 4-day synthetic run says the plumbing is honest, nothing more.
    """
    failed = [name for name, check in checks.items() if not check["ok"]]
    thin = [name for name, check in checks.items() if check.get("thin_margin")]
    if not checks:
        return "no checks ran"
    if failed:
        return "fragile: " + ", ".join(failed) + " failed"
    if thin:
        return "fragile: survives, but the margin is thin on " + ", ".join(thin)
    return "robust to costs and resampling (still subject to the out-of-sample gates)"
