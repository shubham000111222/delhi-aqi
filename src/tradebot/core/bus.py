"""In-process pub/sub used to decouple data, strategies, risk and execution.

Backtests use the same bus with an immediate (inline) subscriber, so the wiring is
identical in backtest, paper and live. Slow subscribers are dropped rather than
allowed to stall the market data path.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from typing import Any

from .logging import get_logger

log = get_logger(__name__)


class EventBus:
    def __init__(self, max_queue: int = 10_000) -> None:
        self.max_queue = max_queue
        self._queues: dict[str, list[asyncio.Queue[Any]]] = defaultdict(list)
        self._inline: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self._dropped = 0

    # --- async consumers -------------------------------------------------
    def subscribe(self, topic: str) -> asyncio.Queue[Any]:
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self.max_queue)
        self._queues[topic].append(q)
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue[Any]) -> None:
        if q in self._queues[topic]:
            self._queues[topic].remove(q)

    def publish(self, topic: str, event: Any) -> None:
        for q in self._queues[topic]:
            if q.full():
                # Drop the oldest event for this consumer only; never block the producer.
                with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover
                    q.get_nowait()
                self._dropped += 1
                log.warning('{"topic":"%s","warning":"subscriber_queue_full_drop"}', topic)
            q.put_nowait(event)
        for fn in self._inline[topic]:
            try:
                fn(event)
            except Exception:  # noqa: BLE001 - one bad listener must not break trading
                log.exception('{"topic":"%s","error":"inline_subscriber_failed"}', topic)

    # --- synchronous consumers (backtest / replay) ------------------------
    def on(self, topic: str, fn: Callable[[Any], None]) -> None:
        self._inline[topic].append(fn)

    async def stream(self, topic: str) -> AsyncIterator[Any]:
        q = self.subscribe(topic)
        try:
            while True:
                yield await q.get()
        finally:
            self.unsubscribe(topic, q)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "topics": {t: len(qs) for t, qs in self._queues.items()},
            "dropped": self._dropped,
        }
