"""The news poller: fetch, parse, dedupe, decay, and what it may do to a size."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from tradebot.ai.sentiment import SentimentStore
from tradebot.core.clock import SimClock
from tradebot.news import NewsPoller, attach

NOW = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
FEED = "https://news.example/rss"


def rss(*items: tuple[str, datetime]) -> str:
    body = "".join(
        f"<item><title>{title}</title><pubDate>{format_datetime(ts)}</pubDate></item>"
        for title, ts in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'


def client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def poller(body: str | None = None, *, status: int = 200, **kwargs: Any) -> NewsPoller:
    async def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            raise httpx.ConnectError("no route", request=request)
        return httpx.Response(status, text=body)

    return NewsPoller(
        urls=kwargs.pop("urls", (FEED,)),
        clock=SimClock(NOW),
        client=client(handler),
        **kwargs,
    )


class TestPolling:
    def test_one_poll_stores_what_it_parsed(self):
        news = poller(rss(("Bitcoin surges to a record", NOW - timedelta(minutes=5))))
        result = asyncio.run(news.poll_once())
        assert result == {"poll": 1, "headlines": 1, "added": 1, "duplicates": 0, "failures": 0}
        assert news.stats.fetched == 1 and news.stats.last_error is None

    def test_the_same_story_in_a_second_poll_is_a_duplicate(self):
        body = rss(("Exchange hacked, funds stolen", NOW - timedelta(minutes=5)))
        news = poller(body)
        asyncio.run(news.poll_once())
        second = asyncio.run(news.poll_once())
        assert second["added"] == 0 and second["duplicates"] == 1
        assert news.stats.polls == 2

    def test_a_feed_that_will_not_load_is_a_count_not_a_crash(self):
        news = poller(None)
        result = asyncio.run(news.poll_once())
        assert result["headlines"] == 0
        assert news.stats.failures == 1
        assert "ConnectError" in (news.stats.last_error or "")

    def test_an_error_page_is_not_parsed_as_news(self):
        news = poller("<html><body>502</body></html>", status=502)
        result = asyncio.run(news.poll_once())
        assert result["headlines"] == 0
        assert news.stats.last_error == "HTTP 502"

    def test_an_item_without_a_date_is_dropped(self):
        body = (
            "<rss><channel><item><title>No date here</title></item>"
            f"<item><title>Dated</title><pubDate>{format_datetime(NOW - timedelta(hours=1))}</pubDate></item>"
            "</channel></rss>"
        )
        news = poller(body)
        result = asyncio.run(news.poll_once())
        assert result["headlines"] == 1, "undated news must not look fresh"

    def test_several_feeds_are_merged_into_one_store(self):
        bodies = {
            "https://a.example/rss": rss(("Bitcoin rallies", NOW - timedelta(minutes=10))),
            "https://b.example/rss": rss(("Exchange exploit drains funds", NOW - timedelta(minutes=10))),
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=bodies[str(request.url)])

        news = NewsPoller(
            urls=tuple(bodies), clock=SimClock(NOW), client=client(handler)
        )
        result = asyncio.run(news.poll_once())
        assert result["headlines"] == 2 and result["added"] == 2

    def test_one_bad_feed_does_not_stop_the_other(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            if "bad" in str(request.url):
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, text=rss(("Bitcoin rallies", NOW - timedelta(minutes=1))))

        news = NewsPoller(
            urls=("https://bad.example/rss", "https://good.example/rss"),
            clock=SimClock(NOW),
            client=client(handler),
        )
        result = asyncio.run(news.poll_once())
        assert result["headlines"] == 1
        assert news.stats.failures == 1

    def test_a_poller_without_feeds_is_refused(self):
        with pytest.raises(ValueError, match="at least one feed"):
            NewsPoller(urls=(), clock=SimClock(NOW))


class TestOverlayEffect:
    def test_bad_news_shrinks_and_good_news_does_not_grow(self):
        news = poller(rss(("Exchange hacked, funds stolen in exploit", NOW - timedelta(minutes=5))))
        asyncio.run(news.poll_once())
        bad = news.overlay.modifier("BTC/USDT", NOW)
        assert bad["multiplier"] < 1.0

        good_news = poller(rss(("Bitcoin surges to a record high", NOW - timedelta(minutes=5))))
        asyncio.run(good_news.poll_once())
        good = good_news.overlay.modifier("BTC/USDT", NOW)
        assert good["multiplier"] == 1.0

    def test_an_empty_store_changes_nothing(self):
        news = poller("<rss><channel></channel></rss>")
        asyncio.run(news.poll_once())
        decision = news.overlay.modifier("BTC/USDT", NOW)
        assert decision["multiplier"] == 1.0 and "no relevant headlines" in decision["reason"]

    def test_old_news_decays_to_nothing(self):
        news = poller(
            rss(("Exchange hacked, funds stolen", NOW - timedelta(hours=2))),
            store=SentimentStore(half_life=timedelta(minutes=30)),
        )
        asyncio.run(news.poll_once())
        much_later = NOW + timedelta(hours=6)
        assert news.overlay.modifier("BTC/USDT", much_later)["multiplier"] == 1.0


class TestRunLoop:
    def test_the_loop_stops_after_the_requested_number_of_polls(self):
        news = poller(rss(("Bitcoin rallies", NOW - timedelta(minutes=1))))
        stats = asyncio.run(news.run(max_polls=3))
        assert stats.polls == 3
        assert stats.added == 1 and stats.duplicates == 2

    def test_the_loop_returns_at_once_when_stop_is_already_set(self):
        news = poller(rss(("Bitcoin rallies", NOW - timedelta(minutes=1))))
        stop = asyncio.Event()
        stop.set()
        stats = asyncio.run(news.run(stop))
        assert stats.polls == 0

    def test_the_loop_exits_when_the_stop_event_fires(self):
        news = poller(rss(("Bitcoin rallies", NOW - timedelta(minutes=1))), interval_seconds=5.0)
        stop = asyncio.Event()

        async def run() -> Any:
            task = asyncio.create_task(news.run(stop))
            await asyncio.sleep(0.05)
            stop.set()
            return await asyncio.wait_for(task, timeout=5)

        stats = asyncio.run(run())
        assert stats.polls == 1


class TestEngineAttachment:
    def test_attach_installs_a_reduce_only_overlay_on_the_engine(self):
        import asyncio as aio
        from decimal import Decimal

        from tradebot.config import load_config
        from tradebot.core.store import Store
        from tradebot.engine import Engine

        cfg = load_config()

        async def build() -> Any:
            return await Engine.build(cfg, clock=SimClock(NOW), store=Store(":memory:"))

        engine = aio.run(build())
        try:
            news = attach(engine, [FEED], interval_seconds=60.0)
            assert isinstance(news, NewsPoller)
            assert engine.sentiment is news.overlay

            # no headlines yet: sizing is exactly what the risk engine decided
            signal = type("S", (), {"symbol": "BTC/USDT", "strategy": "S1", "id": "x"})()
            rules = engine.adapter.get_market_rules("BTC/USDT")
            assert engine._apply_sentiment(signal, Decimal("1"), rules) == Decimal("1")

            # a bad story, and the same order is cut
            from tradebot.ai.sentiment import parse_simple_rss

            news.store.add_many(
                parse_simple_rss(rss(("Exchange hacked, funds stolen in exploit", NOW - timedelta(minutes=5))))
            )
            assert engine._apply_sentiment(signal, Decimal("1"), rules) < Decimal("1")
        finally:
            engine.close()
