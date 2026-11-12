"""Actual isolated four-variant paper engines on frozen supplied candle inputs.

Cold-start strategy windows, modeled books and paper fills are research assumptions,
not recorded execution parity or verified OOS evidence. No live enablement here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ..analysis.engine_gate import ReplayBinding, TimeMachineEngineGate
from ..analysis.strategy_replay import StrategyReplayRule
from ..analysis.timemachine import TimeMachine
from ..config import Config, Mode
from ..core.clock import SimClock, timeframe_seconds
from ..core.models import Candle, KillLevel
from ..core.money import D
from ..core.store import Store
from ..data.feed import ReplayFeed
from ..engine import Engine
from .metrics import (
    GateResult,
    Metrics,
    acceptance_gates,
    all_gates_passed,
    compute_metrics,
    monte_carlo_drawdown,
)
from .runner import collect
from .timemachine_study import VARIANTS, StudyWindow, VariantRun, run_study


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


async def run_engine_study(config: Config, execution_bars: Mapping[str, Sequence[Candle]],
                           history_5m: Mapping[str, Sequence[Candle]], *, start: datetime,
                           end: datetime, final_holdout_start: datetime,
                           fee_rate: float, slippage_rate: float,
                           max_bars: int = 288, cost_stress: bool = False,
                           volatility_scale: float | None = 1.) -> dict[str, Any]:
    """No injected engine/result callback. Each variant starts from fresh capital.

    Input hashes are computed, not accepted as assertions. Archive/venue provenance
    and OOS status remain unverified. Inputs after study end are never used/hashed.
    The checker applies its own per-decision cutoff to the frozen history tuple.
    Fees/slippage supplied here are checker assumptions; engine cost config is
    separately hashed and unchanged across variants. No final holdout is consumed.
    """
    runs: dict[str, VariantRun] = {}
    diagnostics: dict[str, Any] = {}
    performance: dict[str, Metrics] = {}
    try:
        cfg = copy.deepcopy(config)
        if cfg.mode is not Mode.PAPER:
            raise ValueError('engine study requires paper mode')
        if (any(isinstance(v, bool) or not math.isfinite(v) or not 0 <= v < 1
                for v in (fee_rate, slippage_rate))):
            raise ValueError('invalid checker cost assumptions')
        capital = float(cfg.settings.starting_capital_usdt)
        window = StudyWindow(start, end, final_holdout_start, 'pending', 'pending', capital)
        window.validate()
        execution: dict[str, Sequence[Candle]] = {symbol: tuple(b for b in execution_bars.get(symbol, ())
                                   if start <= b.ts and b.close_time <= end)
                     for symbol in cfg.symbol_names()}
        history = {symbol: tuple(b for b in history_5m.get(symbol, ()) if b.close_time < end)
                   for symbol in cfg.symbol_names()}
        if not any(execution.values()):
            raise ValueError('no execution candles in study window')
        seconds = timeframe_seconds(cfg.settings.base_timeframe)
        if start.timestamp() % seconds or end.timestamp() % seconds:
            raise ValueError('study boundaries must align to execution timeframe')
        for symbol, bars in execution.items():
            if not bars or bars[0].ts != start or bars[-1].close_time != end:
                raise ValueError('incomplete execution coverage for ' + symbol)
            if any((b.ts - a.ts).total_seconds() != seconds
                   for a, b in zip(bars, bars[1:], strict=False)):
                raise ValueError('execution history has gaps or duplicates')
            for bar in bars:
                values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
                if (not all(v.is_finite() for v in values) or min(values[:4]) <= 0 or bar.volume < 0
                        or bar.high < max(bar.open, bar.close, bar.low)
                        or bar.low > min(bar.open, bar.close, bar.high)):
                    raise ValueError('invalid execution OHLCV')
            if any(b.symbol != symbol or b.timeframe != cfg.settings.base_timeframe for b in bars):
                raise ValueError('execution bar symbol/timeframe mismatch')
            if any(b.ts >= c.ts for b, c in zip(bars, bars[1:], strict=False)):
                raise ValueError('execution bars not strictly ordered')
        bindings: dict[str | tuple[str, str], ReplayBinding] = {(name, tf): StrategyReplayRule(name, strategy, tf, cfg.strategies.regime,
                                                   max_bars=max_bars).binding()
                    for name, strategy in cfg.enabled_strategies.items() for tf in strategy.timeframes}
        input_hash = _digest({'execution': execution, 'history': history})
        settings_hash = _digest({'settings': cfg.settings.model_dump(mode='json'),
                                 'risk': cfg.risk.model_dump(mode='json'),
                                 'strategies': cfg.strategies.model_dump(mode='json'),
                                 'symbols': cfg.symbols.model_dump(mode='json'),
                                 'checker_costs': [fee_rate, slippage_rate], 'max_bars': max_bars,
                                 'volatility_scale': volatility_scale, 'runner': 'isolated-cold-start-v2'})
        window = StudyWindow(start, end, final_holdout_start, input_hash, settings_hash, capital)
        with TemporaryDirectory(prefix='timemachine-study-') as temporary:
            for variant in VARIANTS:
                run_cfg = copy.deepcopy(cfg)
                root = Path(temporary) / variant
                run_cfg.settings.data_dir = root / 'data'
                run_cfg.settings.state_dir = root / 'state'
                run_cfg.settings.report.dir = str(root / 'reports')
                run_cfg.settings.watchdog.heartbeat_file = str(root / 'heartbeat')
                clock = SimClock(start)
                store = Store(':memory:')
                engine = None
                alerts: list[str] = []
                try:
                    feed = ReplayFeed(run_cfg, execution, clock=clock)
                    engine = await Engine.build(run_cfg, clock=clock, store=store, feed=feed)
                    if variant != 'no_filter':
                        engine.set_research_candidate_gate(TimeMachineEngineGate(
                            TimeMachine(alerts.append), lambda symbol, cutoff: history[symbol], bindings,
                            fee_rate=fee_rate, slippage_rate=slippage_rate, variant=variant,
                            volatility_scale=volatility_scale))
                    # Finite prefiltered feed: drain every symbol at the final close.
                    # stop_at=end would stop after the first symbol at that timestamp.
                    await engine.run(idle_timeout=.01)
                    expected_bars = sum(len(bars) for bars in execution.values())
                    if (feed.emitted != expected_bars or engine.stats.bars != expected_bars
                            or engine.killswitch.level is KillLevel.L3):
                        raise ValueError('incomplete engine execution or lockdown: ' + variant)
                    curve, trades, exposure = collect(store)
                    ending = engine.ledger.equity(engine.marks)
                    performance[variant] = compute_metrics(
                        [(start, cfg.settings.starting_capital_usdt), *curve, (end, ending)], trades,
                        starting_equity=cfg.settings.starting_capital_usdt, ending_equity=ending,
                        tax_rate=cfg.risk.tax_reserve_pct, exposure_series=exposure)
                    mc = monte_carlo_drawdown([float(t.pnl) for t in trades], capital) if trades else None
                    run_id = _digest([variant, input_hash, settings_hash, start, end])
                    runs[variant] = VariantRun(variant, run_id, input_hash, settings_hash, start, end,
                                               capital, tuple(float(t.r_multiple) for t in trades),
                                               (capital, *(float(eq) for _, eq in curve),
                                                float(engine.ledger.equity(engine.marks))))
                    # Separate policy skips from operational/checker errors. An invalid
                    # filter cannot earn retention merely by suppressing losing trades.
                    errors = store.events(kinds=['timemachine_check'], limit=10001)
                    invalid = sum(bool(row['payload'].get('check_failed'))
                                  or row['payload'].get('label') == 'invalid check' for row in errors)
                    if len(errors) > 10000:
                        invalid += 1  # incomplete diagnostic coverage cannot support retention
                    terminal = {
                        'positions': [p.to_dict(engine.marks.get(p.symbol)) for p in engine.ledger.open_positions],
                        'pending_entries': len(engine.om.pending),
                        'venue_open_orders': len(await engine.adapter.get_open_orders()),
                        'stored_open_orders': len(store.open_orders()),
                        'pending_fill': store.has_pending_fill(),
                        'pending_submission': store.has_pending_submission(),
                    }
                    censored = bool(any(terminal.values()))
                    diagnostics[variant] = {'metrics': performance[variant].to_dict(), 'monte_carlo': mc,
                                            'closed_trades': [asdict(trade) for trade in trades[:1000]],
                                            'trade_records_truncated': len(trades) > 1000,
                                            'terminal_state': terminal, 'terminal_censored': censored,
                                            'comparison_eligible': not censored and invalid == 0,
                                            'stats': engine.stats.to_dict(), 'checks': len(errors),
                                            'invalid_checks': invalid, 'alerts': len(alerts),
                                            'candidate_checks': errors[:10000],
                                            'checks_truncated': len(errors) > 10000,
                                            'depth': feed.describe()['depth']}
                finally:
                    if engine is not None:
                        engine.close()
                    else:
                        store.close()
        result = run_study(window, lambda variant, _: runs[variant])
        result.update(input_hash=input_hash, settings_hash=settings_hash, diagnostics=diagnostics,
                      engine_isolation=True, cold_start=True,
                      limitation='Cold-start paper engines with modeled books/fills. Venue provenance, '
                                 'real OOS status, execution parity and latency are not verified.')
        for variant in result['retention_criteria_passed']:
            if (not diagnostics['no_filter']['comparison_eligible']
                    or not diagnostics[variant]['comparison_eligible']):
                result['retention_criteria_passed'][variant] = False
        if cost_stress:
            from .robustness import stress_config

            # A new four-engine run changes modeled fills/fees and candidate decisions.
            # Never subtract fees from nominal outcomes or refit the strategy.
            stressed = await run_engine_study(
                stress_config(cfg, 2.), execution, history, start=start, end=end,
                final_holdout_start=final_holdout_start, fee_rate=fee_rate * 2,
                slippage_rate=slippage_rate * 2, max_bars=max_bars, cost_stress=False,
                volatility_scale=volatility_scale)
            result['cost_stress'] = {'multiplier': 2, 'study': stressed,
                                     'model': '2x paper fees and adverse modeled market slippage; '
                                              'limit prices unchanged; checker fee/slippage assumptions doubled',
                                     'venue_costs_verified': False, 'parameters_refit': False}
            result['stress_survivors'] = {
                variant: bool(result['retention_criteria_passed'][variant]
                              and stressed.get('complete')
                              and stressed['input_hash'] == result['input_hash']
                              and stressed['variants'][variant]['trade_count'] > 0
                              and stressed['variants'][variant]['ending_equity'] > capital
                              and stressed['diagnostics'][variant]['comparison_eligible']
                              and stressed['diagnostics']['no_filter']['comparison_eligible'])
                for variant in result['retention_criteria_passed']}
            if not stressed.get('complete'):
                result['complete'] = False
                result['retention_criteria_passed'] = {v: False for v in result['retention_criteria_passed']}
        # Existing project gates remain separate from the narrower retention screen.
        for variant in VARIANTS:
            diagnostic = diagnostics[variant]
            stressed_metrics = None
            if cost_stress:
                stressed_study = result.get('cost_stress', {}).get('study', {})
                if (stressed_study.get('complete') and stressed_study.get('input_hash') == input_hash
                        and stressed_study['diagnostics'][variant]['comparison_eligible']
                        and stressed_study['diagnostics']['no_filter']['comparison_eligible']):
                    stressed_metrics = Metrics(total_return=D(str(
                        stressed_study['diagnostics'][variant]['metrics']['total_return'])))
            gates = acceptance_gates(performance[variant], mc=diagnostic['monte_carlo'],
                                     cost_stress_metrics=stressed_metrics)
            gates.append(GateResult('study_comparison_integrity',
                                     diagnostic['comparison_eligible']
                                     and diagnostics['no_filter']['comparison_eligible'],
                                     'requires valid checks and uncensored baseline/variant terminal state'))
            diagnostic['acceptance_gates'] = [gate.to_dict() for gate in gates]
            diagnostic['acceptance_gates_passed'] = all_gates_passed(gates)
        result['acceptance_gates_passed'] = all(row['acceptance_gates_passed'] for row in diagnostics.values())
        return result
    except Exception as exc:  # noqa: BLE001 - failed research never authorizes trading
        return {'complete': False, 'filter_enabled': False, 'provenance_verified': False,
                'retention_criteria_passed': {v: False for v in VARIANTS if v != 'no_filter'},
                'diagnostics': diagnostics, 'error': type(exc).__name__ + ': ' + str(exc)}
