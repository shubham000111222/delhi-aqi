"""Poll a news feed and keep the sentiment overlay fed.

`ai/sentiment.py` has the parts - a lexicon (or FinBERT) scorer, a deduplicating and
time-decaying store, an overlay that can only shrink a position - but nothing ever
connected them to a running engine. This is that connection: a small poller that fetches
a feed on an interval and hands the parsed headlines to the store.

It lives outside `ai/` for the same reason `meta_gate.py` does: it needs an HTTP client and
the engine's clock, and the AI layer is not allowed to import engine code.

Three rules, all of them learned from how news feeds fail:

* **A failed poll is not a fault.** Feeds time out, rate-limit and return HTML error pages.
  Every failure is counted and logged, and the loop continues; the overlay simply keeps
  decaying the headlines it already has.
* **Nothing is parsed as trusted XML.** `parse_simple_rss` reads text nodes with regexes
  and drops items without a timezone-aware date, so a feed cannot inject entities, and old
  news cannot masquerade as fresh.
* **The overlay can only reduce size.** It is attached to the engine as a modifier in
  [0.5, 1.0]; a broken feed leaves sizing exactly as the risk engine decided it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from .ai.sentiment import (
    Headline,
    LexiconScorer,
    SentimentOverlay,
    SentimentScorer,
    SentimentStore,
    parse_simple_rss,
)
from .core.clock import Clock
from .core.logging import get_logger

log = get_logger(__name__)


@dataclass
class PollerStats:
    polls: int = 0
    fetched: int = 0
    parsed: int = 0
    added: int = 0
    duplicates: int = 0
    failures: int = 0
    last_error: str | None = None
    last_poll_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "polls": self.polls,
            "fetched": self.fetched,
            "parsed": self.parsed,
            "added": self.added,
            "duplicates": self.duplicates,
            "failures": self.failures,
            "last_error": self.last_error,
            "last_poll_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
        }


@dataclass
class NewsPoller:
    """Fetch RSS feeds on an interval and fold them into one sentiment store."""

    urls: tuple[str, ...]
    clock: Clock
    client: httpx.AsyncClient | None = None
    store: SentimentStore = field(default_factory=SentimentStore)
    interval_seconds: float = 300.0
    timeout_seconds: float = 10.0
    stats: PollerStats = field(default_factory=PollerStats)
    #: The overlay handed to the engine. One instance, so the engine's view and the
    #: poller's view cannot drift apart (a property returning a new overlay each access
    #: looked identical in tests but made `engine.sentiment is poller.overlay` false).
    overlay: SentimentOverlay = field(init=False)

    def __post_init__(self) -> None:
        if not self.urls:
            raise ValueError("a news poller needs at least one feed URL")
        self.overlay = SentimentOverlay(store=self.store)

    async def _fetch(self, url: str) -> str | None:
        try:
            if self.client is not None:
                response = await self.client.get(url, timeout=self.timeout_seconds)
            else:  # pragma: no cover - needs network
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, timeout=self.timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - a feed outage is a count, not a crash
            self.stats.failures += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning('{"event":"news_fetch_failed","url":"%s","error":"%s"}', url, type(exc).__name__)
            return None
        if response.status_code != 200:
            self.stats.failures += 1
            self.stats.last_error = f"HTTP {response.status_code}"
            log.warning('{"event":"news_fetch_failed","url":"%s","status":%d}', url, response.status_code)
            return None
        self.stats.fetched += 1
        return response.text

    async def poll_once(self) -> dict[str, Any]:
        """One pass over every feed. Returns what it did, whether or not it worked."""
        self.stats.polls += 1
        self.stats.last_poll_at = self.clock.now()
        batch: list[Headline] = []
        for url in self.urls:
            body = await self._fetch(url)
            if body is None:
                continue
            batch.extend(parse_simple_rss(body, source=url))
        self.stats.parsed += len(batch)
        added = self.store.add_many(batch)
        self.stats.added += added
        self.stats.duplicates += len(batch) - added
        return {
            "poll": self.stats.polls,
            "headlines": len(batch),
            "added": added,
            "duplicates": len(batch) - added,
            "failures": self.stats.failures,
        }

    async def run(self, stop: asyncio.Event | None = None, *, max_polls: int | None = None) -> PollerStats:
        """Poll until cancelled, until ``stop`` is set, or until ``max_polls`` passes.

        With ``max_polls`` the polls run back to back - it exists for tests and for a
        one-shot refresh, and waiting ``interval_seconds`` between them would turn a
        five-second check into a five-minute one.
        """
        while True:
            if stop is not None and stop.is_set():
                break
            await self.poll_once()
            if max_polls is not None and self.stats.polls >= max_polls:
                break
            if max_polls is not None:
                continue
            try:
                if stop is not None:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
                    break
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            except TimeoutError:
                continue
        return self.stats


def attach(
    engine: Any,
    urls: list[str] | tuple[str, ...],
    *,
    scorer: SentimentScorer | None = None,
    interval_seconds: float = 300.0,
    half_life: timedelta = timedelta(hours=6),
    client: httpx.AsyncClient | None = None,
) -> NewsPoller:
    """Install a poller's overlay on the engine. Returns the poller, not a task.

    The engine only ever sees `modifier(symbol, now)`, so an overlay with no headlines yet
    (normal for the first few seconds) changes nothing.
    """
    store = SentimentStore(half_life=half_life, scorer=scorer or LexiconScorer())
    poller = NewsPoller(urls=tuple(urls), clock=engine.clock, client=client, store=store,
                        interval_seconds=interval_seconds)
    engine.sentiment = poller.overlay
    log.info('{"event":"sentiment_attached","feeds":%d,"interval_s":%.0f}', len(poller.urls), interval_seconds)
    return poller


__all__ = ["NewsPoller", "PollerStats", "attach"]
