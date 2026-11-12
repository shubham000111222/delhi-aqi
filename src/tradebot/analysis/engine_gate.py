"""Explicit research bridge from engine candidates to TimeMachine.check.

History providers must load recorded 5m bars, not the short indicator ring buffer.
Replay rules must reproduce the strategy's point-in-time entry/stop/target rule;
this adapter deliberately does not invent a generic replacement strategy.
"""
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..core.models import Candle, OrderType, Side, Signal
from .timemachine import ReplayPlan, TimeMachine


@dataclass(frozen=True)
class ReplayBinding:
    rule: Callable[[Sequence[Candle]], ReplayPlan | None]
    version: str
    max_bars: int = 288


class TimeMachineEngineGate:
    """Attach explicitly with Engine.set_research_candidate_gate; never live config.

    The provider receives the decision cutoff. The checker independently excludes
    future bars. Callers must supply immutable/versioned rule settings and costs.
    Failed providers/bindings propagate to the engine's audited SKIP handler.
    """

    def __init__(self, machine: TimeMachine,
                 history: Callable[[str, datetime], Sequence[Candle]],
                 bindings: Mapping[str | tuple[str, str], ReplayBinding], *, fee_rate: float,
                 slippage_rate: float, volatility_scale: float = 1., coin_flip_skip: bool = True,
                 variant: str = 'both'):
        if variant not in ('both', 'simulation_only', 'replay_only'):
            raise ValueError('unsupported research check variant')
        self.variant = variant
        self.machine = machine
        self.history = history
        self.bindings = dict(bindings)
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate
        self.volatility_scale = volatility_scale
        self.coin_flip_skip = coin_flip_skip

    def __call__(self, signal: Signal, decision_time: datetime, mark: Decimal) -> dict[str, object]:
        if signal.entry_type is not OrderType.MARKET:
            raise ValueError('Time Machine research bridge supports market entries only')
        if signal.ts > decision_time:
            raise ValueError('candidate timestamp is in the future')
        binding = self.bindings.get((signal.strategy, signal.timeframe), self.bindings.get(signal.strategy))
        if binding is None or not binding.version:
            raise ValueError('explicit versioned strategy replay binding required')
        plan = ReplayPlan(float(mark), float(signal.stop), float(signal.target),
                          1 if signal.side is Side.BUY else -1, binding.max_bars)
        plan.validate()
        result = self.machine.check(
            lambda: self.history(signal.symbol, decision_time), signal.symbol, decision_time,
            plan, signal.id, binding.rule, fee_rate=self.fee_rate,
            slippage_rate=self.slippage_rate, rule_version=binding.version,
            volatility_scale=self.volatility_scale, coin_flip_skip=self.coin_flip_skip, variant=self.variant)
        return {**result, 'strategy': signal.strategy, 'symbol': signal.symbol,
                'decision_time': decision_time.isoformat(), 'rule_version': binding.version,
                'fee_rate': self.fee_rate, 'slippage_rate': self.slippage_rate,
                'volatility_scale': self.volatility_scale, 'max_bars': binding.max_bars,
                'entry_reference': str(mark), 'stop': str(signal.stop), 'target': str(signal.target),
                'side': signal.side.value}
