"""Exchange adapters. Paper is the reference implementation; live mirrors it."""

from __future__ import annotations

from typing import Any

from ...config import Config, Mode
from ...core.clock import Clock
from ...data.hub import MarketData
from .base import ExchangeAdapter, ExchangeError, PermanentExchangeError, TransientExchangeError
from .paper import PaperAdapter

__all__ = [
    "ExchangeAdapter",
    "ExchangeError",
    "PermanentExchangeError",
    "TransientExchangeError",
    "PaperAdapter",
    "make_adapter",
]


def make_adapter(config: Config, market: MarketData, clock: Clock, rules: dict[str, Any] | None = None) -> ExchangeAdapter:
    """Paper for paper mode; ccxt for testnet and live (each with its own keys)."""
    if config.mode is Mode.PAPER:
        return PaperAdapter(
            market=market,
            rules=rules or {},
            cfg=config.settings.paper,
            clock=clock,
            quote_currency=config.settings.quote_currency,
            starting_cash=config.settings.starting_capital_usdt,
            seed=config.settings.synthetic.seed,
        )
    from .ccxt_live import CcxtLiveAdapter

    return CcxtLiveAdapter(config, market, clock, testnet=config.mode is Mode.TESTNET)
