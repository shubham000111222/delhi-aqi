"""Performance metrics computed from an equity curve and a trade list.

Everything is annualised on crypto's 365-day year and reported both pre- and
post-tax, because India taxes VDA gains at a flat 30% with no loss set-off: a
strategy that looks fine pre-tax can be a loser after it.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..core.money import D, to_str

TRADING_DAYS = 365


@dataclass
class TradeRecord:
    symbol: str
    strategy: str
    pnl: Decimal
    r_multiple: Decimal
    opened_at: datetime
    closed_at: datetime
    fees: Decimal = Decimal("0")
    reason: str = ""

    @property
    def hold_hours(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds() / 3600.0

    @property
    def win(self) -> bool:
        return self.pnl > 0


@dataclass
class Metrics:
    total_return: Decimal = Decimal("0")
    annualised_return: Decimal = Decimal("0")
    sharpe: Decimal = Decimal("0")
    sortino: Decimal = Decimal("0")
    calmar: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")
    max_drawdown_days: float = 0.0
    win_rate: Decimal = Decimal("0")
    profit_factor: Decimal = Decimal("0")
    expectancy_r: Decimal = Decimal("0")
    trade_count: int = 0
    avg_win: Decimal = Decimal("0")
    avg_loss: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    turnover: Decimal = Decimal("0")
    exposure_pct: Decimal = Decimal("0")
    time_in_market_pct: Decimal = Decimal("0")
    by_strategy: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_symbol: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_month: dict[str, Decimal] = field(default_factory=dict)
    post_tax_return: Decimal = Decimal("0")
    tax_reserve: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if isinstance(value, Decimal):
                out[key] = float(value)
            elif isinstance(value, dict) and key == "by_month":
                out[key] = {k: float(v) for k, v in value.items()}
            else:
                out[key] = value
        return out

    def summary_lines(self) -> list[str]:
        return [
            f"return {self.total_return * 100:+.2f}% (annualised {self.annualised_return * 100:+.1f}%, "
            f"post-tax {self.post_tax_return * 100:+.2f}%)",
            f"sharpe {self.sharpe:.2f} | sortino {self.sortino:.2f} | calmar {self.calmar:.2f}",
            f"max drawdown {self.max_drawdown * 100:.2f}% over {self.max_drawdown_days:.0f} days",
            f"trades {self.trade_count} | win {self.win_rate * 100:.1f}% | PF {self.profit_factor:.2f} | "
            f"expectancy {self.expectancy_r:+.3f}R",
            f"fees {self.fees:.2f} | turnover {self.turnover:.0f} | time in market {self.time_in_market_pct:.0f}%",
        ]


def _daily_returns(curve: list[tuple[datetime, Decimal]]) -> list[float]:
    """Last equity value per UTC day, converted to simple daily returns."""
    if len(curve) < 2:
        return []
    per_day: dict[str, Decimal] = {}
    for ts, equity in curve:
        per_day[ts.date().isoformat()] = equity
    days = [per_day[k] for k in sorted(per_day)]
    returns = []
    for prev, cur in zip(days, days[1:], strict=False):
        if prev > 0:
            returns.append(float((cur - prev) / prev))
    return returns


def compute_metrics(
    curve: list[tuple[datetime, Decimal]],
    trades: list[TradeRecord],
    *,
    starting_equity: Decimal,
    ending_equity: Decimal,
    tax_rate: Decimal = Decimal("0.30"),
    risk_free: float = 0.0,
    exposure_series: list[float] | None = None,
    bars: int = 0,
) -> Metrics:
    m = Metrics()
    if starting_equity > 0:
        m.total_return = (ending_equity - starting_equity) / starting_equity

    returns = _daily_returns(curve)
    if returns:
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1)
        sd = math.sqrt(var)
        downside = [min(0.0, r - risk_free / TRADING_DAYS) for r in returns]
        dsd = math.sqrt(sum(d * d for d in downside) / len(downside))
        m.sharpe = D(sd and (mean - risk_free / TRADING_DAYS) / sd * math.sqrt(TRADING_DAYS))
        m.sortino = D(dsd and (mean - risk_free / TRADING_DAYS) / dsd * math.sqrt(TRADING_DAYS))
        years = max(len(returns) / TRADING_DAYS, 1 / TRADING_DAYS)
        m.annualised_return = D((1 + float(m.total_return)) ** (1 / years) - 1)

    # drawdown and its duration
    peak = starting_equity
    worst = Decimal("0")
    dd_start: datetime | None = None
    longest = 0.0
    for ts, equity in curve:
        if equity >= peak:
            peak = equity
            if dd_start is not None:
                longest = max(longest, (ts - dd_start).total_seconds() / 86400)
                dd_start = None
        else:
            if dd_start is None:
                dd_start = ts
            dd = (equity - peak) / peak if peak > 0 else Decimal("0")
            worst = min(worst, dd)
    if dd_start is not None and curve:
        longest = max(longest, (curve[-1][0] - dd_start).total_seconds() / 86400)
    m.max_drawdown = worst
    m.max_drawdown_days = longest
    m.calmar = D(float(m.annualised_return) / abs(float(worst))) if worst else Decimal("0")

    # trade stats
    m.trade_count = len(trades)
    wins = [t for t in trades if t.win]
    losses = [t for t in trades if not t.win]
    m.win_rate = D(len(wins) / len(trades)) if trades else Decimal("0")
    gross_profit = sum((t.pnl for t in wins), Decimal("0"))
    gross_loss = sum((t.pnl for t in losses), Decimal("0"))
    m.profit_factor = D(float(gross_profit) / abs(float(gross_loss))) if gross_loss else Decimal("0")
    m.expectancy_r = (
        sum((t.r_multiple for t in trades), Decimal("0")) / len(trades) if trades else Decimal("0")
    )
    m.avg_win = gross_profit / len(wins) if wins else Decimal("0")
    m.avg_loss = gross_loss / len(losses) if losses else Decimal("0")
    m.fees = sum((t.fees for t in trades), Decimal("0"))
    m.turnover = D(sum(abs(float(t.pnl)) for t in trades))

    for dimension, attr in (("by_strategy", "strategy"), ("by_symbol", "symbol")):
        buckets: dict[str, dict[str, Any]] = {}
        for t in trades:
            key = getattr(t, attr)
            row = buckets.setdefault(key, {"trades": 0, "wins": 0, "pnl": Decimal("0"), "r": Decimal("0")})
            row["trades"] += 1
            row["wins"] += int(t.win)
            row["pnl"] += t.pnl
            row["r"] += t.r_multiple
        for row in buckets.values():
            row["win_rate"] = float(row["wins"] / row["trades"]) if row["trades"] else 0.0
            row["pnl"] = float(row["pnl"])
            row["avg_r"] = float(row["r"] / row["trades"]) if row["trades"] else 0.0
            row.pop("r", None)
        setattr(m, dimension, buckets)

    months: dict[str, Decimal] = {}
    for t in trades:
        key = t.closed_at.strftime("%Y-%m")
        months[key] = months.get(key, Decimal("0")) + t.pnl
    m.by_month = months

    if exposure_series:
        m.exposure_pct = D(sum(exposure_series) / len(exposure_series))
        m.time_in_market_pct = D(sum(1 for e in exposure_series if e > 0) / len(exposure_series) * 100)

    # tax: 30% on net gains, no loss set-off (conservative: tax the positive sum only)
    taxable = max(m.total_return * starting_equity, Decimal("0"))
    m.tax_reserve = taxable * tax_rate
    m.post_tax_return = (
        (ending_equity - m.tax_reserve - starting_equity) / starting_equity if starting_equity else Decimal("0")
    )
    del bars
    return m


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------
def monte_carlo_drawdown(
    trade_pnls: list[float],
    starting_equity: float,
    *,
    iterations: int = 1000,
    seed: int = 7,
    sample_fraction: float = 1.0,
) -> dict[str, float]:
    """Bootstrap trade order to get a confidence band on drawdown and return."""
    if not trade_pnls:
        return {"p50_drawdown": 0.0, "p95_drawdown": 0.0, "p50_return": 0.0, "p05_return": 0.0}
    rng = random.Random(seed)
    drawdowns, returns = [], []
    n = max(1, int(len(trade_pnls) * sample_fraction))
    for _ in range(iterations):
        sample = [rng.choice(trade_pnls) for _ in range(n)]
        equity = starting_equity
        peak = equity
        worst = 0.0
        for pnl in sample:
            equity += pnl
            peak = max(peak, equity)
            worst = min(worst, (equity - peak) / peak if peak else 0.0)
        drawdowns.append(worst)
        returns.append((equity - starting_equity) / starting_equity if starting_equity else 0.0)
    drawdowns.sort()
    returns.sort()

    def q(values: list[float], pct: float) -> float:
        idx = min(len(values) - 1, max(0, int(round(pct * (len(values) - 1)))))
        return values[idx]

    return {
        "p50_drawdown": q(drawdowns, 0.50),
        "p95_drawdown": q(drawdowns, 0.05),  # 5th percentile of a negative number = worse
        "p50_return": q(returns, 0.50),
        "p05_return": q(returns, 0.05),
    }


def deflated_sharpe(
    sharpe: float,
    trials: int,
    observations: int,
    *,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Deflated Sharpe as a z-score: penalise the best of many attempted variants.

    Bailey & López de Prado / Bailey et al. (2014). The benchmark is the Sharpe you would
    expect to see *by luck* from ``trials`` attempts, and the divisor is the non-normality
    correction ``sqrt(1 - skew*SR + ((kurtosis - 1)/4)*SR^2)``. Pass the skew and
    (non-excess) kurtosis of the same returns and the correction is applied; with the
    defaults (0 and 3) it reduces to the normal case.

    This is a screening tool, not a substitute for the holdout: it only makes luck less
    convincing. Use `psr_probability` when you want the probability rather than a z-score.
    """
    if observations <= 0 or trials <= 0:
        return 0.0
    # E[max of n N(0,1)] ~ (1 - gamma) * Phi^-1(1 - 1/n) + gamma * Phi^-1(1 - 1/(n e))
    gamma = 0.5772156649
    e_max = (1 - gamma) * _norm_ppf(1 - 1.0 / trials) + gamma * _norm_ppf(1 - 1.0 / (trials * math.e))
    sr = sharpe
    benchmark = e_max / math.sqrt(observations)
    denom = math.sqrt(max(1e-9, 1.0 - skew * sr + ((kurtosis - 1.0) / 4.0) * sr * sr))
    return float((sr - benchmark) / denom)


def return_moments(returns: Sequence[float]) -> tuple[float, float]:
    """Skew and (non-excess) kurtosis of a return series; (0, 3) when there is too little data."""
    values = [float(v) for v in returns]
    n = len(values)
    if n < 4:
        return 0.0, 3.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    if var <= 0:
        return 0.0, 3.0
    sd = math.sqrt(var)
    skew = sum(((v - mean) / sd) ** 3 for v in values) / n
    kurt = sum(((v - mean) / sd) ** 4 for v in values) / n
    return float(skew), float(kurt)


def psr_probability(z: float) -> float:
    """Turn a deflated-Sharpe z-score into a probability using the normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Acklam's inverse normal CDF approximation (enough for screening)."""
    if p <= 0:
        return -math.inf
    if p >= 1:
        return math.inf
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
    )


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


GATES = {
    "profit_factor": 1.3,
    "sharpe": 1.0,
    "max_drawdown": 0.15,
    "min_trades": 200,
    "monte_carlo_p95_drawdown": 0.20,
}


def acceptance_gates(
    metrics: Metrics,
    *,
    mc: dict[str, float] | None = None,
    cost_stress_metrics: Metrics | None = None,
    gates: dict[str, float] | None = None,
) -> list[GateResult]:
    g = dict(GATES)
    g.update(gates or {})
    results = [
        GateResult(
            "out_of_sample_profit_factor",
            float(metrics.profit_factor) >= g["profit_factor"],
            f"PF {float(metrics.profit_factor):.2f} >= {g['profit_factor']}",
        ),
        GateResult(
            "sharpe",
            float(metrics.sharpe) >= g["sharpe"],
            f"sharpe {float(metrics.sharpe):.2f} >= {g['sharpe']}",
        ),
        GateResult(
            "max_drawdown",
            abs(float(metrics.max_drawdown)) <= g["max_drawdown"],
            f"max DD {float(metrics.max_drawdown) * 100:.2f}% <= {g['max_drawdown'] * 100:.0f}%",
        ),
        GateResult(
            "trade_count",
            metrics.trade_count >= g["min_trades"],
            f"{metrics.trade_count} trades >= {g['min_trades']:.0f}",
        ),
    ]
    if mc is not None:
        results.append(
            GateResult(
                "monte_carlo_drawdown",
                abs(mc["p95_drawdown"]) <= g["monte_carlo_p95_drawdown"],
                f"MC p95 DD {mc['p95_drawdown'] * 100:.2f}% <= {g['monte_carlo_p95_drawdown'] * 100:.0f}%",
            )
        )
    if cost_stress_metrics is not None:
        results.append(
            GateResult(
                "cost_stress_2x",
                cost_stress_metrics.total_return > 0,
                f"return at 2x costs {float(cost_stress_metrics.total_return) * 100:+.2f}% > 0",
            )
        )
    return results


def all_gates_passed(results: list[GateResult]) -> bool:
    return all(r.passed for r in results)


def format_gates(results: list[GateResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"{'PASS' if r.passed else 'FAIL'}  {r.name:28s} {r.detail}")
    return "\n".join(lines)


def metrics_from_store(store: Any, model_id: str) -> dict[str, Any]:  # pragma: no cover
    raise NotImplementedError("use compute_metrics with the equity curve and trade list instead")


__all__ = [
    "Metrics",
    "TradeRecord",
    "compute_metrics",
    "monte_carlo_drawdown",
    "deflated_sharpe",
    "psr_probability",
    "return_moments",
    "acceptance_gates",
    "all_gates_passed",
    "format_gates",
    "GateResult",
    "to_str",
]
