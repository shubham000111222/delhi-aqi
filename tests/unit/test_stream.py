"""Tests for the streaming feed and its decoder.

The transport and the frames are the only things faked here; everything else - the
reconnect loop, the gap fill, the staleness check, the event contract - is the code that
would run in production.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from tradebot.config import FeedKind, load_config
from tradebot.core.clock import SimClock
from tradebot.data.feed import make_feed
from tradebot.data.stream import (
    FeedHealth,
    WebSocketFeed,
    decode_binance,
    decode_generic,
    replay_transport,
    summarise_events,
)


async def _noop(kind: str, payload: Any) -> None:
    return None


def kline_frame(symbol: str = "BTC/USDT", *, close: float = 100.0, closed: bool = True,
                open_ms: int = 1704067200000) -> dict[str, Any]:
    return {
        "stream": f"{symbol.replace('/', '').lower()}@kline_1m",
        "data": {
            "e": "kline",
            "s": symbol.replace("/", ""),
            "k": {
                "t": open_ms,
                "T": open_ms + 59_999,
                "s": symbol.replace("/", ""),
                "i": "1m",
                "o": "99.0",
                "c": str(close),
                "h": "101.0",
                "l": "98.0",
                "v": "12.5",
                "x": closed,
            },
        },
    }


def depth_frame(symbol: str = "BTC/USDT") -> dict[str, Any]:
    return {
        "data": {
            "e": "depth20",
            "s": symbol.replace("/", ""),
            "E": 1704067200000,
            "bids": [["100.0", "2.0"], ["99.9", "1.0"]],
            "asks": [["100.1", "1.5"], ["100.2", "3.0"]],
        }
    }


class TestDecoder:
    def test_a_closed_kline_becomes_a_bar(self):
        events = decode_binance(kline_frame())
        assert len(events) == 1
        kind, candle = events[0]
        assert kind == "bar"
        assert str(candle.close) == "100.0"
        assert candle.timeframe == "1m"
        assert candle.ts.tzinfo is not None

    def test_an_unfinished_kline_is_ignored(self):
        assert decode_binance(kline_frame(closed=False)) == []

    def test_a_partial_book_becomes_a_book(self):
        kind, book = decode_binance(depth_frame())[0]
        assert kind == "book"
        assert book.best_bid is not None and book.best_ask is not None

    def test_a_trade_becomes_a_tick_with_the_right_side(self):
        frame = {"data": {"e": "trade", "s": "BTCUSDT", "p": "100.5", "q": "0.2",
                          "T": 1704067200000, "m": True}}
        kind, trade = decode_binance(frame)[0]
        assert kind == "tick"
        assert trade.taker_side.value == "sell", "m=True means the buyer was the maker"

    def test_json_text_is_accepted(self):
        assert decode_binance(json.dumps(kline_frame())) != []

    def test_garbage_is_ignored_not_raised(self):
        assert decode_binance("not json") == []
        assert decode_binance({"data": {"e": "unknown"}}) == []
        assert decode_binance(42) == []
        assert decode_binance({"data": {"e": "kline", "k": {}}}) == []

    def test_an_impossible_candle_is_rejected(self):
        frame = kline_frame()
        frame["data"]["k"]["h"] = "90.0"  # high below low
        assert decode_binance(frame) == []

    def test_the_generic_decoder_reads_our_own_replay_format(self):
        frame = {"kind": "bar", "symbol": "BTC/USDT", "ts": "2024-01-01T00:00:00+00:00",
                 "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "10"}
        kind, candle = decode_generic(frame)[0]
        assert kind == "bar" and candle.ts.tzinfo is not None

    def test_the_generic_decoder_rejects_a_broken_frame(self):
        assert decode_generic({"kind": "bar"}) == []


class TestFeed:
    def _feed(self, frames: list[Any], **kwargs: Any) -> tuple[WebSocketFeed, list]:
        cfg = load_config()
        clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
        kwargs.setdefault("reconnect", False)  # a recording ends; it does not reconnect
        feed = WebSocketFeed(cfg, clock, transport=replay_transport(frames), **kwargs)
        events: list[tuple[str, Any]] = []

        async def emit(kind: str, payload: Any) -> None:
            events.append((kind, payload))

        return feed, events, emit  # type: ignore[return-value]

    def test_it_emits_the_engine_contract_and_then_stops(self):
        feed, events, emit = self._feed([kline_frame(), depth_frame()])
        asyncio.run(feed.run(emit))
        kinds = [kind for kind, _ in events]
        assert kinds == ["bar", "book"]
        assert feed.health.frames == 2
        assert feed.health.events == 2
        assert feed.health.decode_errors == 0

    def test_exchange_symbols_are_rewritten_to_the_engines_symbols(self):
        """Binance streams `BTCUSDT`; the engine keys on `BTC/USDT`."""
        feed, events, emit = self._feed([kline_frame(), depth_frame()])
        asyncio.run(feed.run(emit))
        for kind, payload in events:
            assert payload.symbol == "BTC/USDT", kind

    def test_a_broken_transport_raises_on_replay_instead_of_spinning(self):
        cfg = load_config()
        clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))

        async def transport(streams):  # noqa: ARG001
            raise ConnectionError("no route to host")
            yield  # pragma: no cover - makes this an async generator

        feed = WebSocketFeed(cfg, clock, transport=transport, reconnect=False)
        with pytest.raises(ConnectionError):
            asyncio.run(feed.run(_noop))
        assert "ConnectionError" in (feed.health.last_error or "")

    def test_bad_frames_are_counted_not_fatal(self):
        feed, events, emit = self._feed([kline_frame(), "garbage", depth_frame()])
        asyncio.run(feed.run(emit))
        assert len(events) == 2
        assert feed.health.decode_errors == 1

    def test_without_a_transport_it_refuses_loudly(self):
        cfg = load_config()
        feed = WebSocketFeed(cfg, SimClock(datetime(2024, 1, 1, tzinfo=UTC)))

        async def emit(kind: str, payload: Any) -> None:
            return None

        with pytest.raises(RuntimeError, match="needs a transport"):
            asyncio.run(feed.run(emit))

    def test_a_dropped_connection_reconnects_and_fills_the_gap(self):
        cfg = load_config()
        clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
        attempts = {"count": 0}
        filled: list[Any] = []

        async def transport(streams):  # noqa: ARG001
            attempts["count"] += 1
            if attempts["count"] == 1:
                yield kline_frame()
                raise ConnectionError("socket closed")
            yield kline_frame(close=101.0)

        async def gap_filler(symbol, since, until):
            from tradebot.core.models import Candle
            from tradebot.core.money import D

            candle = Candle(
                symbol=symbol, timeframe="1m", ts=since,
                open=D("100"), high=D("102"), low=D("99"), close=D("101"), volume=D("5"),
            )
            filled.append(candle)
            return [candle]

        feed = WebSocketFeed(cfg, clock, transport=transport, gap_filler=gap_filler,
                             max_backoff_seconds=0.0, max_reconnects=2)
        events: list[tuple[str, Any]] = []

        async def emit(kind, payload):
            events.append((kind, payload))

        async def run():
            feed._running = True
            task = asyncio.create_task(feed.run(emit))
            await asyncio.wait_for(task, timeout=5)

        asyncio.run(run())
        assert attempts["count"] >= 2
        assert feed.health.reconnects >= 1
        assert filled, "the gap filler should have been consulted"
        assert feed.health.gaps_filled >= 1

    def test_the_gap_window_covers_the_outage_even_with_a_frozen_clock(self):
        """The replay clock never moves; the gap window must follow the frames instead."""
        cfg = load_config()
        clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
        windows: list[tuple[datetime, datetime]] = []
        attempts = {"n": 0}
        base_ms = 1_704_067_200_000  # 2024-01-01T00:00Z

        async def transport(streams):  # noqa: ARG001
            attempts["n"] += 1
            offset = 0 if attempts["n"] == 1 else 4 * 60_000
            yield kline_frame(open_ms=base_ms + offset)

        async def gap_filler(symbol: str, since: datetime, until: datetime):
            windows.append((since, until))
            return []

        feed = WebSocketFeed(cfg, clock, transport=transport, gap_filler=gap_filler,
                             max_backoff_seconds=0.0, max_reconnects=1)
        asyncio.run(feed.run(_noop))
        assert windows, "a second connection must trigger a gap fill"
        since, until = windows[0]
        assert until > since, "an empty window would silently skip the missing bars"

    def test_staleness_is_reported(self):
        cfg = load_config()
        clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
        feed = WebSocketFeed(cfg, clock, stale_after_seconds=30)
        assert feed.is_stale() is True, "no frames yet is stale"
        feed.health.last_frame_at = clock.now()
        assert feed.is_stale() is False
        clock.advance(60)
        assert feed.is_stale() is True
        assert feed.health.stale_alerts == 1

    def test_describe_reports_the_transport_and_health(self):
        feed, _events, emit = self._feed([kline_frame()])
        asyncio.run(feed.run(emit))
        described = feed.describe()
        assert described["transport"] == "injected"
        assert described["health"]["events"] == 1


class TestWiring:
    def test_the_config_can_ask_for_the_stream_feed(self):
        cfg = load_config()
        cfg.settings.feed = FeedKind.STREAM
        feed = make_feed(cfg)
        assert isinstance(feed, WebSocketFeed)
        assert feed.transport is not None, "a stream feed without a transport cannot run"
        assert feed.gap_filler is not None, "a stream that cannot backfill hides outages"
        assert feed.describe()["reconnect"] is True

    def test_the_other_feed_kinds_are_untouched(self):
        cfg = load_config()
        cfg.settings.feed = FeedKind.SYNTHETIC
        assert make_feed(cfg).name == "synthetic"


class TestStats:
    def test_events_are_summarised_by_kind(self):
        events = decode_binance(kline_frame()) + decode_binance(depth_frame())
        stats = summarise_events(events).as_dict()
        assert stats["bars"] == 1 and stats["books"] == 1
        assert stats["symbols"]

    def test_health_serialises_cleanly(self):
        health = FeedHealth(frames=3, events=2, last_frame_at=datetime(2024, 1, 1, tzinfo=UTC))
        payload = health.as_dict()
        assert payload["frames"] == 3
        assert payload["last_frame_at"].startswith("2024-01-01")
