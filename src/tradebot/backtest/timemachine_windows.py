"""Predeclared multi-window Time Machine research; no tuning or promotion path."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import Config
from ..core.models import Candle
from .timemachine_engine_study import run_engine_study
from .timemachine_study import VARIANTS, StudyWindow


@dataclass(frozen=True)
class ResearchWindow:
    start: datetime
    end: datetime


def validate_windows(windows: Sequence[ResearchWindow], holdout: datetime) -> tuple[ResearchWindow, ...]:
    if not 3 <= len(windows) <= 24:
        raise ValueError('predeclare 3..24 research windows')
    frozen = tuple(windows)
    for index, window in enumerate(frozen):
        StudyWindow(window.start, window.end, holdout, 'pending', 'pending', 1.).validate()
        if index and frozen[index-1].end > window.start:
            raise ValueError('research windows must be ordered and non-overlapping')
    return frozen


async def run_window_studies(config: Config, execution: Mapping[str, Sequence[Candle]],
                             history: Mapping[str, Sequence[Candle]], *,
                             windows: Sequence[ResearchWindow], final_holdout_start: datetime,
                             fee_rate: float, slippage_rate: float, max_bars: int = 288,
                             cost_stress: bool = True, volatility_scale: float | None = 1., replay_days: int = 60) -> dict[str, Any]:
    """Independent cold-start portfolios per window; never concatenate their drawdowns.

    Conservative research screen: a variant must pass retention and actual modeled
    stress profitability in EVERY declared window. No best-window/variant selection.
    Passed screens are not verified OOS provenance or the project's promotion gates.
    """
    studies: list[dict[str, Any]] = []
    eligible = {v: False for v in VARIANTS if v != 'no_filter'}
    try:
        frozen = validate_windows(windows, final_holdout_start)
        # Snapshot containers once: later caller appends cannot change window inputs.
        execution = {symbol: tuple(bars) for symbol, bars in execution.items()}
        history = {symbol: tuple(bars) for symbol, bars in history.items()}
        import copy

        snapshot = copy.deepcopy(config)
        expected_settings = None
        for window in frozen:
            study = await run_engine_study(snapshot, execution, history, start=window.start, end=window.end,
                                           final_holdout_start=final_holdout_start, fee_rate=fee_rate,
                                           slippage_rate=slippage_rate, max_bars=max_bars, cost_stress=cost_stress,
                                           volatility_scale=volatility_scale, replay_days=replay_days)
            studies.append({'start': window.start.isoformat(), 'end': window.end.isoformat(), 'study': study})
            if not study.get('complete'):
                raise ValueError('incomplete research window: ' + str(study.get('error', 'cost stress failed')))
            if expected_settings is None:
                expected_settings = study['settings_hash']
            elif expected_settings != study['settings_hash']:
                raise ValueError('settings changed between research windows')
        for variant in eligible:
            eligible[variant] = all(row['study']['retention_criteria_passed'][variant]
                                    and row['study'].get('stress_survivors', {}).get(variant, False)
                                    for row in studies)
        return {'complete': True, 'windows': studies, 'research_screen_passed': eligible,
                'settings_hash': expected_settings, 'parameters_refit': False,
                'holdout_used': False, 'filter_enabled': False, 'provenance_verified': False,
                'rule': 'retention and profitable modeled 2x cost rerun in every predeclared window',
                'limitation': 'Independent cold-start portfolios and modeled fills. Not verified OOS '
                              'evidence or promotion approval; no pooled drawdown or best-window selection.'}
    except Exception as exc:  # noqa: BLE001 - incomplete evidence never authorizes entry
        return {'complete': False, 'windows': studies,
                'research_screen_passed': {v: False for v in eligible},
                'filter_enabled': False, 'provenance_verified': False,
                'error': type(exc).__name__ + ': ' + str(exc)}
