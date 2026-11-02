"""Token bucket rate limiter. Stays well under exchange limits by design."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class TokenBucket:
    """Classic token bucket: `rate` tokens per `period` seconds, burst = rate."""

    def __init__(
        self,
        rate: float,
        period: float = 1.0,
        utilization: float = 0.5,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        if rate <= 0 or period <= 0:
            raise ValueError("rate and period must be positive")
        self.capacity = max(1.0, rate * utilization)
        self.rate = self.capacity / period
        self.tokens = self.capacity
        self.now_fn = now_fn or time.monotonic
        self.updated = self.now_fn()

    def _refill(self) -> None:
        now = self.now_fn()
        elapsed = now - self.updated
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def time_until(self, tokens: float = 1.0) -> float:
        self._refill()
        if self.tokens >= tokens:
            return 0.0
        return (tokens - self.tokens) / self.rate


class SlidingWindowCounter:
    """Counts events in a trailing window (orders/minute, rejects/5min).

    The time source is injectable so a backtest measures windows on *market* time:
    replaying two days of trades in two real seconds must not look like a rate storm.
    """

    def __init__(self, window_seconds: float, now_fn: Callable[[], float] | None = None) -> None:
        self.window = window_seconds
        self.now_fn = now_fn or time.monotonic
        self._events: deque[float] = deque()

    def _trim(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def add(self, ts: float | None = None) -> None:
        now = self.now_fn() if ts is None else ts
        self._events.append(now)
        self._trim(now)

    def count(self, ts: float | None = None) -> int:
        self._trim(self.now_fn() if ts is None else ts)
        return len(self._events)

    def clear(self) -> None:
        self._events.clear()
