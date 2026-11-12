"""Turns what the engine recorded into metrics, robustness checks and gate verdicts.

Used by `tradebot backtest` and `tradebot metrics`. It reads only the store, so a
paper session and a backtest are evaluated with exactly the same code.
"""

from __future__ import annotations

import math
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


class EvidenceDataError(ValueError):
    """Required recorded metrics evidence is missing or invalid."""


def _parse(ts: str | None) -> datetime:
    from datetime import UTC

    if not isinstance(ts, str) or not ts:
        raise EvidenceDataError('recorded timestamp is required; wall-clock fallback forbidden')
    try:
        stamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('naive timestamp')
        return stamp.astimezone(UTC)
    except ValueError as exc:
        raise EvidenceDataError('invalid or timezone-naive evidence timestamp') from exc


def _number(value: Any, field: str) -> Decimal:
    try:
        if value is None or isinstance(value, bool):
            raise ValueError('missing or boolean metric')
        number = D(value)
        if not number.is_finite() or not math.isfinite(float(number)):
            raise ValueError('nonfinite metric')
        return number
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise EvidenceDataError('missing or invalid recorded ' + field) from exc


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
    curve: list[tuple[datetime, Decimal]] = []
    exposure: list[float] = []
    for point in points:
        stamp = _parse(point.get('ts'))
        if curve and stamp < curve[-1][0]:
            raise EvidenceDataError('equity evidence is not chronological')
        equity = _number(point.get('equity'), 'equity')
        positions = _number(point.get('positions_value'), 'positions_value')
        curve.append((stamp, equity))
        ratio = float(positions / equity) if equity else 0.
        if not math.isfinite(ratio):
            raise EvidenceDataError('nonfinite exposure ratio')
        exposure.append(ratio)
    trades: list[TradeRecord] = []
    for row in closed:
        opened, ended = _parse(row.get('opened_at')), _parse(row.get('closed_at'))
        if ended < opened:
            raise EvidenceDataError('closed trade ends before it opens')
        if any(not isinstance(row.get(k), str) or not row[k].strip() for k in ('symbol', 'strategy')):
            raise EvidenceDataError('recorded trade symbol and strategy are required')
        trades.append(TradeRecord(
            symbol=row['symbol'], strategy=row['strategy'],
            pnl=_number(row.get('pnl'), 'trade pnl'),
            r_multiple=_number(row.get('r_multiple'), 'trade R'),
            opened_at=opened, closed_at=ended, fees=_number(row.get('fees'), 'trade fees'),
            reason=str(row.get('exit_reason') or '')))
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
