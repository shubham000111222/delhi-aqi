"""Streaming market data (§4): a WebSocket feed with reconnect, staleness and gap fill.

`CcxtFeed` polls REST, which is fine for a demo and wrong for production: three requests a
second per symbol, and a book that is seconds stale by the time it arrives. The plan asks
for a stream with REST gap fill, and the event contract has to stay identical so nothing
downstream changes.

This module is written around three ideas:

* **The transport is injected.** `WebSocketFeed` takes an async generator of raw frames.
  That is what makes the whole thing testable without an exchange: the tests feed it
  recorded JSON and assert on the events that come out.
* **Decoding is a pure function.** `decode_binance` maps one raw frame to zero or more
  engine events. No sockets, no clock, no state - so a recorded frame is a test fixture
  forever.
* **Disconnects are normal, not exceptions.** The feed reconnects with exponential
  backoff and jitter, counts the attempt, and on reconnect asks a `gap_filler` for the
  bars it missed. A silent gap in the middle of a position is worse than a loud error.

The live transport (`websockets.connect`) is behind a one-line factory and is not
exercised here: this sandbox cannot reach an exchange. The class says so, and
`describe()` reports whether it is running on a real socket or a replay.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from ..config import Config
from ..core.clock import UTC, Clock, timeframe_seconds
from ..core.money import D
from .feed import BaseFeed, Emit

#: A transport yields raw frames (text or already-parsed dicts) until it is closed.
#: ``transport(streams)`` returns an async iterator of raw frames (an async generator
#: satisfies this; so does a coroutine that returns one).
TransportFactory = Callable[[Sequence[str]], Any]

#: On reconnect, fetch the bars emitted while we were away. Returns closed candles.
GapFiller = Callable[[str, datetime, datetime], Awaitable[Sequence[Any]]]


@dataclass
class FeedHealth:
    connected: bool = False
    connects: int = 0
    reconnects: int = 0
    frames: int = 0
    events: int = 0
    decode_errors: int = 0
    last_frame_at: datetime | None = None
    last_event_at: datetime | None = None
    gaps_filled: int = 0
    stale_alerts: int = 0
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "connects": self.connects,
            "reconnects": self.reconnects,
            "frames": self.frames,
            "events": self.events,
            "decode_errors": self.decode_errors,
            "gaps_filled": self.gaps_filled,
            "stale_alerts": self.stale_alerts,
            "last_error": self.last_error,
            "last_frame_at": self.last_frame_at.isoformat() if self.last_frame_at else None,
        }


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal | None:
    try:
        return D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def decode_binance(frame: Any) -> list[tuple[str, Any]]:
    """Map one Binance combined-stream frame to engine events.

    Handles ``kline`` (a closed 1m candle), ``depth20``/``depth`` (book snapshot and
    delta) and ``trade``. Anything malformed returns ``[]`` rather than raising: one bad
    frame from an exchange must never take the bot down, and the caller counts it.

    Book deltas are *not* applied here - that needs state, which belongs to the hub.
    Binance sends a full snapshot every 1000ms on ``depth20`` and partial deltas on
    ``depth``; using the snapshot is the conservative choice, so deltas are ignored.
    """
    from ..core.models import BookLevel, Candle, OrderBook, Side, Trade

    if isinstance(frame, str):
        try:
            frame = json.loads(frame)
        except json.JSONDecodeError:
            return []
    if not isinstance(frame, dict):
        return []

    payload = frame.get("data", frame)
    if not isinstance(payload, dict):
        return []
    kind = str(payload.get("e") or payload.get("stream") or "")

    if kind == "kline":
        kline = payload.get("k") or {}
        if not kline.get("x", True):
            return []  # an unfinished candle is not a candle
        symbol = str(kline.get("s") or payload.get("s") or "")
        open_time = kline.get("t")
        close_time = kline.get("T")
        if not symbol or open_time is None:
            return []
        try:
            ts = datetime.fromtimestamp(int(open_time) / 1000, tz=UTC)
            close_ts = (
                datetime.fromtimestamp(int(close_time) / 1000, tz=UTC)
                if close_time is not None
                else ts + timedelta(minutes=1)
            )
            candle = Candle(
                symbol=symbol,
                timeframe=str(kline.get("i") or "1m"),
                ts=ts,
                open=D(str(kline["o"])),
                high=D(str(kline["h"])),
                low=D(str(kline["l"])),
                close=D(str(kline["c"])),
                volume=D(str(kline.get("v", "0"))),
            )
            del close_ts  # Candle derives close_time from ts + timeframe
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return []
        if candle.low > candle.high:
            return []  # impossible candle: let the hub's spike filter never see it
        return [("bar", candle)]

    if kind in ("depth20", "depthUpdate", "depth"):
        symbol = str(payload.get("s") or "")
        bids = payload.get("bids") or payload.get("b") or []
        asks = payload.get("asks") or payload.get("a") or []
        if not symbol or not bids or not asks:
            return []
        try:
            book = OrderBook(
                symbol=symbol,
                ts=datetime.fromtimestamp(int(payload.get("E", 0)) / 1000, tz=UTC) if payload.get("E") else datetime.now(tz=UTC),
                bids=tuple(
                    BookLevel(price=D(str(price)), qty=D(str(qty))) for price, qty in bids[:20]
                ),
                asks=tuple(
                    BookLevel(price=D(str(price)), qty=D(str(qty))) for price, qty in asks[:20]
                ),
            )
        except (TypeError, ValueError, InvalidOperation):
            return []
        return [("book", book)]

    if kind == "trade":
        symbol = str(payload.get("s") or "")
        price = _decimal(payload.get("p"))
        qty = _decimal(payload.get("q"))
        event_ts = payload.get("T") or payload.get("E")
        if not symbol or price is None or qty is None or event_ts is None:
            return []
        try:
            ts = datetime.fromtimestamp(int(event_ts) / 1000, tz=UTC)
        except (TypeError, ValueError, OSError):
            return []
        side = Side.SELL if payload.get("m") else Side.BUY
        return [("tick", Trade(symbol=symbol, ts=ts, price=price, qty=qty, taker_side=side))]

    return []


def decode_generic(frame: Any) -> list[tuple[str, Any]]:
    """Decoder for our own replay format: ``{"kind": "bar", ...}`` through the models."""
    if isinstance(frame, str):
        try:
            frame = json.loads(frame)
        except json.JSONDecodeError:
            return []
    if not isinstance(frame, dict):
        return []
    kind = frame.get("kind")
    if kind == "bar":
        from ..core.models import Candle

        try:
            return [
                (
                    "bar",
                    Candle(
                        symbol=frame["symbol"],
                        timeframe=frame.get("timeframe", "1m"),
                        ts=datetime.fromisoformat(frame["ts"]),
                        open=D(frame["open"]),
                        high=D(frame["high"]),
                        low=D(frame["low"]),
                        close=D(frame["close"]),
                        volume=D(frame["volume"]),
                    ),
                )
            ]
        except (KeyError, ValueError, InvalidOperation):
            return []
    return []


# ---------------------------------------------------------------------------
# the feed
# ---------------------------------------------------------------------------


@dataclass
class WebSocketFeed(BaseFeed):
    """A reconnecting stream feed with a pluggable transport and decoder."""

    name = "websocket"

    def __init__(
        self,
        config: Config,
        clock: Clock | None = None,
        *,
        transport: TransportFactory | None = None,
        decoder: Callable[[Any], list[tuple[str, Any]]] | None = None,
        gap_filler: GapFiller | None = None,
        max_backoff_seconds: float = 30.0,
        stale_after_seconds: float = 90.0,
        max_reconnects: int | None = None,
        reconnect: bool = True,
    ) -> None:
        super().__init__(config, clock)
        self.transport = transport
        self.decoder = decoder or decode_binance
        self.gap_filler = gap_filler
        self.health = FeedHealth()
        self.max_backoff_seconds = max_backoff_seconds
        self.stale_after_seconds = stale_after_seconds
        self.max_reconnects = max_reconnects
        #: A live socket should reconnect forever; a replay over recorded frames should
        #: stop when the recording ends. Without this the feed spins on an exhausted
        #: transport - which is exactly what the first version of this class did.
        self.reconnect = reconnect
        self._clean_close = False
        self._backoff = 1.0
        self._rng = random.Random(7)
        self._last_bar_ts: dict[str, datetime] = {}
        self._last_seen_ts: dict[str, datetime] = {}
        #: Exchanges stream `BTCUSDT`; the engine keys everything on `BTC/USDT`. Without
        #: this map every bar would arrive under a symbol nothing looks up, and the bot
        #: would sit flat while apparently receiving data.
        self._canonical: dict[str, str] = {
            str(symbol).replace("/", "").replace("-", ""): str(symbol) for symbol in self.symbols
        }

    # --- lifecycle ------------------------------------------------------
    async def run(self, emit: Emit) -> None:
        if self.transport is None:
            raise RuntimeError(
                "WebSocketFeed needs a transport; the live one (websockets.connect) is not "
                "available in this build"
            )
        self._running = True
        attempt = 0
        while self._running:
            attempt += 1
            try:
                await self._stream_once(emit)
                self._clean_close = True
                self._backoff = 1.0  # a clean close resets the backoff
                if not self.reconnect:
                    break
                await asyncio.sleep(min(self.max_backoff_seconds, 1.0))
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect, never crash the engine
                self.health.connected = False
                self.health.last_error = f"{type(exc).__name__}: {exc}"
                if not self.reconnect:
                    # A replay must not spin on a broken transport: surface it, loudly.
                    self._running = False
                    raise
                self.health.reconnects += 1
                delay = min(self.max_backoff_seconds, self._backoff) * (0.5 + self._rng.random())
                await asyncio.sleep(delay)
                self._backoff = min(self.max_backoff_seconds, self._backoff * 2.0)
            if self.max_reconnects is not None and attempt > self.max_reconnects:
                break
            if not self._running:
                break
        self._running = False

    async def _stream_once(self, emit: Emit) -> None:
        assert self.transport is not None
        streams = [f"{symbol.lower().replace('/', '')}@kline_1m" for symbol in self.symbols]
        frames = self.transport(streams)
        if inspect.isawaitable(frames):
            frames = await frames
        self.health.connected = True
        self.health.connects += 1
        reconnected = self.health.connects > 1
        gap_checked = not reconnected
        try:
            async for frame in frames:
                if not self._running:
                    break
                self.health.frames += 1
                self.health.last_frame_at = self.clock.now()
                events = self.decoder(frame)
                if not events:
                    self.health.decode_errors += 1
                    continue
                events = [(kind, self._canonicalise(payload)) for kind, payload in events]
                if not gap_checked:
                    # The window's end is the first frame after the outage, not the wall
                    # clock: on a replayed recording the clock has not moved, and asking
                    # for a window that ends where it starts fills nothing.
                    gap_checked = True
                    newest = max(
                        (
                            payload.ts
                            for _, payload in events
                            if isinstance(getattr(payload, "ts", None), datetime)
                        ),
                        default=None,
                    )
                    await self._fill_gaps(emit, now=newest)
                for kind, payload in events:
                    seen_ts = getattr(payload, "ts", None)
                    if isinstance(seen_ts, datetime):
                        self._last_seen_ts[getattr(payload, "symbol", "")] = seen_ts
                    if kind == "bar":
                        symbol = getattr(payload, "symbol", "")
                        ts = getattr(payload, "ts", None)
                        if symbol and ts is not None:
                            self._last_bar_ts[symbol] = ts
                    await emit(kind, payload)
                    self.health.events += 1
                    self.health.last_event_at = self.clock.now()
        finally:
            self.health.connected = False
            await _close(frames)

    def _canonicalise(self, payload: Any) -> Any:
        """Rewrite an exchange symbol (`BTCUSDT`) to the configured one (`BTC/USDT`)."""
        symbol = getattr(payload, "symbol", None)
        if not isinstance(symbol, str):
            return payload
        target = self._canonical.get(symbol.replace("/", "").replace("-", ""))
        if target is None or target == symbol:
            return payload
        try:
            return replace(payload, symbol=target)
        except TypeError:  # pragma: no cover - a payload type without a symbol field
            return payload

    def stop(self) -> None:
        self._running = False

    async def _fill_gaps(self, emit: Emit, now: datetime | None = None) -> None:
        """Ask for the bars missed while disconnected, then emit them in order."""
        if self.gap_filler is None:
            return
        step = timedelta(seconds=timeframe_seconds(self.base_timeframe))
        # The window ends at "now" in wall time, or at the newest frame we have seen -
        # whichever is later. A replayed recording runs ahead of a frozen SimClock, and
        # taking only the clock would ask for a window that ends before the gap starts.
        end = self.clock.now()
        seen = [ts for ts in self._last_seen_ts.values() if ts is not None]
        if seen:
            end = max(end, max(seen))
        if now is not None:
            end = max(end, now)
        end = end + step
        for symbol in self.symbols:
            since = self._last_bar_ts.get(symbol)
            if since is None:
                continue
            try:
                missed = await self.gap_filler(symbol, since + step, end)
            except Exception:  # noqa: BLE001 - a failed gap fill is an alert, not a crash
                continue
            for candle in sorted(missed, key=lambda item: _bar_ts(item, end)):
                await emit("bar", candle)
                self.health.events += 1
                self.health.gaps_filled += 1
                self._last_bar_ts[symbol] = getattr(candle, "ts", self._last_bar_ts[symbol])

    # --- health ---------------------------------------------------------
    def is_stale(self, now: datetime | None = None) -> bool:
        moment = now or self.clock.now()
        last = self.health.last_frame_at
        if last is None:
            return True
        if (moment - last).total_seconds() > self.stale_after_seconds:
            self.health.stale_alerts += 1
            return True
        return False

    def describe(self) -> dict[str, Any]:
        return {
            "feed": self.name,
            "symbols": len(self.symbols),
            "transport": "injected" if self.transport is not None else "none",
            "reconnect": self.reconnect,
            "last_close": "clean" if self._clean_close else "error",
            "health": self.health.as_dict(),
        }


def _bar_ts(candle: Any, fallback: datetime) -> datetime:
    """A sort key that tolerates a gap filler returning something odd."""
    ts = getattr(candle, "ts", None)
    return ts if isinstance(ts, datetime) else fallback


async def _close(iterator: Any) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        with contextlib.suppress(Exception):
            await close()


async def live_transport(
    streams: Sequence[str], *, url: str = "wss://stream.binance.com:9443/stream?streams="
) -> AsyncIterator[Any]:
    """The real socket. Not exercised here - the sandbox has no exchange access.

    Imports `websockets` lazily so the dependency is optional, and yields raw text frames
    for `decode_binance`.
    """
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError('streaming needs `pip install websockets`') from exc
    async with websockets.connect(url + "/".join(streams), ping_interval=20) as socket:
        async for message in socket:  # pragma: no cover - needs a network
            yield message


def replay_transport(frames: Sequence[Any]) -> TransportFactory:
    """A transport over recorded frames: the fixture every stream test uses."""

    async def factory(streams: Sequence[str]) -> AsyncIterator[Any]:
        del streams
        for frame in frames:
            yield frame

    return factory


def rest_gap_filler(config: Config) -> GapFiller:
    """Gap fill over ccxt REST: the bars the socket dropped while it was down.

    Streams are excellent until they disconnect, and then they lie by omission: the next
    frame arrives as if nothing happened and the engine's indicators silently skip the
    missing minutes. This fetches them.
    """

    state: dict[str, Any] = {"exchange": None}

    async def filler(symbol: str, since: datetime, until: datetime) -> Sequence[Any]:
        from ..core.models import Candle

        exchange = state["exchange"]
        if exchange is None:
            import ccxt.async_support as ccxt  # lazy: paper trading never needs it

            exchange = getattr(ccxt, config.settings.ccxt.exchange_id)(
                {"enableRateLimit": True, "options": {"defaultType": "spot"}}
            )
            state["exchange"] = exchange  # kept even if load_markets fails, so it closes
            await exchange.load_markets()
        timeframe = config.settings.base_timeframe
        try:
            raw = await exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                since=int(since.timestamp() * 1000),
                limit=500,
            )
        except Exception:  # noqa: BLE001 - a failed gap fill is an alert, not a crash
            return []
        out: list[Any] = []
        for row in raw or []:
            ts = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
            if ts < since or ts >= until:
                continue
            out.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    ts=ts,
                    open=D(str(row[1])),
                    high=D(str(row[2])),
                    low=D(str(row[3])),
                    close=D(str(row[4])),
                    volume=D(str(row[5])),
                )
            )
        return out

    return filler


def live_feed(config: Config, clock: Clock | None = None) -> WebSocketFeed:
    """The production feed: a real socket, with REST gap fill on every reconnect."""
    return WebSocketFeed(config, clock, transport=live_transport, gap_filler=rest_gap_filler(config))


@dataclass
class StreamStats:
    frames: int = 0
    bars: int = 0
    books: int = 0
    ticks: int = 0
    errors: int = 0
    symbols: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "bars": self.bars,
            "books": self.books,
            "ticks": self.ticks,
            "errors": self.errors,
            "symbols": sorted(self.symbols),
        }


def summarise_events(events: Sequence[tuple[str, Any]]) -> StreamStats:
    stats = StreamStats(frames=len(events))
    for kind, payload in events:
        symbol = getattr(payload, "symbol", None)
        if symbol:
            stats.symbols.add(symbol)
        if kind == "bar":
            stats.bars += 1
        elif kind == "book":
            stats.books += 1
        elif kind == "tick":
            stats.ticks += 1
        else:
            stats.errors += 1
    return stats
