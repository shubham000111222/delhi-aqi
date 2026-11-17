"""Four independent Time Machine research runs; never production enablement.

The supplied backtest runner must enforce decision-time inputs and run the ordinary
risk engine. A declared OOS window/hash is not independently verified provenance.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

Variant = Literal['no_filter', 'simulation_only', 'replay_only', 'both']
VARIANTS: tuple[Variant, ...] = ('no_filter', 'simulation_only', 'replay_only', 'both')


@dataclass(frozen=True)
class StudyWindow:
    start: datetime
    end: datetime
    final_holdout_start: datetime
    input_hash: str
    settings_hash: str
    starting_capital: float

    def validate(self) -> None:
        if any(t.tzinfo is None or t.utcoffset() is None for t in
               (self.start, self.end, self.final_holdout_start)):
            raise ValueError('study timestamps must be aware')
        if not self.start < self.end <= self.final_holdout_start:
            raise ValueError('study window overlaps holdout or is invalid')
        if (not self.input_hash or not self.settings_hash or isinstance(self.starting_capital, bool)
                or not math.isfinite(self.starting_capital) or self.starting_capital <= 0):
            raise ValueError('invalid study inputs')


@dataclass(frozen=True)
class VariantRun:
    # Computed by an independent engine/backtest run, not reweighted baseline trades.
    variant: Variant
    run_id: str
    input_hash: str
    settings_hash: str
    start: datetime
    end: datetime
    starting_capital: float
    # Closed-trade net R after fees/slippage; actual equity marks include open risk.
    net_r: tuple[float, ...]
    equity: tuple[float, ...]


def _metrics(run: VariantRun, window: StudyWindow) -> dict[str, Any]:
    if (run.variant not in VARIANTS or not run.run_id
            or (run.input_hash, run.settings_hash, run.start, run.end, run.starting_capital)
            != (window.input_hash, window.settings_hash, window.start, window.end, window.starting_capital)):
        raise ValueError('incomparable backtest inputs')
    if (not run.equity or run.equity[0] != window.starting_capital
            or any(isinstance(v, bool) or not math.isfinite(v) for v in (*run.net_r, *run.equity))
            or any(v < 0 for v in run.equity)):
        raise ValueError('invalid trade/equity results')
    peak = run.equity[0]
    drawdown = 0.
    for equity in run.equity:
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak-equity)/peak)
    return {'run_id': run.run_id, 'trade_count': len(run.net_r),
            'net_expectancy_r': sum(run.net_r)/len(run.net_r) if run.net_r else None,
            'max_drawdown': drawdown, 'ending_equity': run.equity[-1]}


def run_study(window: StudyWindow, run_variant: Callable[[Variant, StudyWindow], VariantRun]) -> dict[str, Any]:
    """Run all variants from identical capital/window/settings; fail closed on errors.

    Callback must create fresh engine/store/portfolio per variant. Unique run IDs
    help diagnose accidental reuse but cannot attest that implementation property.
    Errors produce an incomplete study with all retention decisions disabled.
    """
    results: dict[str, Any] = {}
    retention = {name: False for name in VARIANTS if name != 'no_filter'}
    try:
        window.validate()
        ids: set[str] = set()
        for variant in VARIANTS:
            run = run_variant(variant, window)
            if run.variant != variant or run.run_id in ids:
                raise ValueError('reused run identity or incorrect variant')
            ids.add(run.run_id)
            results[variant] = _metrics(run, window)
        base = results['no_filter']
        for variant in retention:
            row = results[variant]
            enough = base['trade_count'] > 0 and row['trade_count'] >= base['trade_count']*.5
            improved = (row['trade_count'] > 0 and base['net_expectancy_r'] is not None and
                        (row['net_expectancy_r'] > base['net_expectancy_r']
                         or row['max_drawdown'] < base['max_drawdown']))
            retention[variant] = bool(enough and improved)
        return {'complete': True, 'variants': results, 'retention_criteria_passed': retention,
                'filter_enabled': False, 'provenance_verified': False,
                'holdout_used': False,
                'limitation': 'Research comparison only. Caller runner provenance, point-in-time inputs, '
                              'fresh run isolation and unchanged risk enforcement still require verification.'}
    except Exception as exc:  # noqa: BLE001 - incomplete evidence never authorizes enablement
        return {'complete': False, 'variants': results, 'retention_criteria_passed': retention,
                'filter_enabled': False, 'provenance_verified': False,
                'error': type(exc).__name__ + ': ' + str(exc)}
