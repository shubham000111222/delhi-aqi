"""Builds strategy instances from strategies.yaml."""

from __future__ import annotations

import importlib

from ..config import StrategiesFile, StrategyConfig
from .base import Strategy, StrategyParams


def _import_class(path: str) -> type[Strategy]:
    module_name, _, class_name = path.partition(":")
    if not module_name or not class_name:
        raise ValueError(f"strategy class must be 'module:Class', got {path!r}")
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    if not issubclass(cls, Strategy):
        raise TypeError(f"{path} is not a Strategy subclass")
    return cls


def build_strategy(name: str, cfg: StrategyConfig) -> Strategy:
    cls = _import_class(cfg.class_name)
    params = StrategyParams(
        name=name,
        timeframes=tuple(cfg.timeframes),
        regimes=tuple(cfg.regimes),
        priority=cfg.priority,
        raw=dict(cfg.params),
    )
    strategy = cls(params)
    strategy.name = name
    return strategy


def build_all(file: StrategiesFile, only_enabled: bool = True) -> dict[str, Strategy]:
    out: dict[str, Strategy] = {}
    for name, cfg in file.strategies.items():
        if only_enabled and not cfg.enabled:
            continue
        out[name] = build_strategy(name, cfg)
    return out
