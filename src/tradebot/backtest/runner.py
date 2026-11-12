"""Turns what the engine recorded into metrics, robustness checks and gate verdicts.

Used by `tradebot backtest` and `tradebot metrics`. It reads only the store, so a
paper session and a backtest are evaluated with exactly the same code.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from ..config import Config
from ..core.money import D
from ..core.store import Store
from .metrics import (
    GateResult,
    TradeRecord,
    acceptance_gates,
    all_gates_passed,
    compute_metrics,
    monte_carlo_drawdown,
)


def _parse(ts: str | None) -> datetime:
    from datetime import UTC

    if not ts:
        return datetime.now(UTC)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


class EvidenceLimitError(ValueError):
    """Metrics cannot be computed from a silently truncated evidence ledger."""


def collect(store: Store, *, max_records: int = 100_000) -> tuple[list[tuple[datetime, Decimal]], list[TradeRecord], list[float]]:
    """Collect complete bounded inputs or refuse; never calculate from a tail page.

    One extra row detects overflow in both tables. This is independent of bounded
    display/audit pages, which may truncate explicitly without changing metrics.
    """
    if type(max_records) is not int or not 1 <= max_records <= 100_000:
        raise ValueError('metrics evidence limit must be1..100000 records')
    points = store.equity_series(limit=max_records + 1)
    if len(points) > max_records:
        raise EvidenceLimitError('equity evidence exceeds metrics record limit')
    closed = store.closed_positions(limit=max_records + 1)
    if len(closed) > max_records:
        raise EvidenceLimitError('closed-trade evidence exceeds metrics record limit')
    curve = [(_parse(p["ts"]), p["equity"]) for p in points]
    exposure = [float(p["positions_value"] / p["equity"]) if p["equity"] else 0.0 for p in points]
    trades: list[TradeRecord] = []
    for row in closed:
        trades.append(
            TradeRecord(
                symbol=str(row.get("symbol")),
                strategy=str(row.get("strategy")),
                pnl=D(row.get("pnl") or "0"),
                r_multiple=D(row.get("r_multiple") or "0"),
                opened_at=_parse(row.get("opened_at")),
                closed_at=_parse(row.get("closed_at")),
                fees=D(row.get("fees") or "0"),
                reason=str(row.get("exit_reason") or ""),
            )
        )
    return curve, trades, exposure


def evaluate(store: Store, config: Config, starting_equity: Decimal | None = None) -> dict[str, Any]:
    curve, trades, exposure = collect(store)
    start = starting_equity or D(str(store.kv_get("initial_equity", config.settings.starting_capital_usdt)))
    end = curve[-1][1] if curve else start
    metrics = compute_metrics(
        curve,
        trades,
        starting_equity=start,
        ending_equity=end,
        tax_rate=config.risk.tax_reserve_pct,
        exposure_series=exposure,
    )
    mc = monte_carlo_drawdown([float(t.pnl) for t in trades], float(start))
    gates: list[GateResult] = acceptance_gates(metrics, mc=mc)
    return {
        "metrics": metrics,
        "monte_carlo": mc,
        "gates": gates,
        "passed": all_gates_passed(gates),
        "summary": metrics.summary_lines(),
        "trades": trades,
    }
