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
from ..core.store import Store
from ..data.feed import ReplayFeed
from ..engine import Engine
from .runner import collect
from .timemachine_study import VARIANTS, StudyWindow, VariantRun, run_study


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


async def run_engine_study(config: Config, execution_bars: Mapping[str, Sequence[Candle]],
                           history_5m: Mapping[str, Sequence[Candle]], *, start: datetime,
                           end: datetime, final_holdout_start: datetime,
                           fee_rate: float, slippage_rate: float,
                           max_bars: int = 288) -> dict[str, Any]:
    """No injected engine/result callback. Each variant starts from fresh capital.

    Input hashes are computed, not accepted as assertions. Archive/venue provenance
    and OOS status remain unverified. Inputs after study end are never used/hashed.
    The checker applies its own per-decision cutoff to the frozen history tuple.
    Fees/slippage supplied here are checker assumptions; engine cost config is
    separately hashed and unchanged across variants. No final holdout is consumed.
    """
    runs: dict[str, VariantRun] = {}
    diagnostics: dict[str, Any] = {}
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
                                 'runner': 'isolated-cold-start-v1'})
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
                            fee_rate=fee_rate, slippage_rate=slippage_rate, variant=variant))
                    # Finite prefiltered feed: drain every symbol at the final close.
                    # stop_at=end would stop after the first symbol at that timestamp.
                    await engine.run(idle_timeout=.01)
                    expected_bars = sum(len(bars) for bars in execution.values())
                    if (feed.emitted != expected_bars or engine.stats.bars != expected_bars
                            or engine.killswitch.level is KillLevel.L3):
                        raise ValueError('incomplete engine execution or lockdown: ' + variant)
                    curve, trades, _ = collect(store)
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
                    diagnostics[variant] = {'stats': engine.stats.to_dict(), 'checks': len(errors),
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
            if diagnostics[variant]['invalid_checks']:
                result['retention_criteria_passed'][variant] = False
        return result
    except Exception as exc:  # noqa: BLE001 - failed research never authorizes trading
        return {'complete': False, 'filter_enabled': False, 'provenance_verified': False,
                'retention_criteria_passed': {v: False for v in VARIANTS if v != 'no_filter'},
                'diagnostics': diagnostics, 'error': type(exc).__name__ + ': ' + str(exc)}
