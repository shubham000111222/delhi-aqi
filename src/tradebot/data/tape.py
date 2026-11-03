"""Record a session's feed events and replay them byte-for-byte.

Candles are not enough to reproduce a session. A limit order fills when price trades
through it, and slippage depends on how much size sits in the book — neither is
recoverable from OHLC. Recording the whole tape (bars, ticks and books, in order) gives
a replay that is indistinguishable from the original, which is what makes a parity test
meaningful and what §18.1 asks for ("record your own live books from day 1").

Two small adapters do the work:

* ``RecordingFeed`` wraps any feed, forwards every event untouched, and keeps a copy.
* ``TapeFeed`` emits a tape back through the same dispatch path, stepping the clock to
  each event's own timestamp.

Nothing here changes engine behaviour: the recording is a side effect of the normal run,
and a replay is the normal run over recorded input.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..config import Config
from ..core.clock import Clock, iso, utc
from ..core.models import Candle, MarketRules, OrderBook, Side, Trade
from ..core.money import D
from .feed import BaseFeed, Emit

#: Event kinds the engine understands from a feed.
TAPE_KINDS = ("bar", "tick", "book")


def _event_ts(payload: Any) -> datetime | None:
    for attribute in ("close_time", "ts"):
        value = getattr(payload, attribute, None)
        if isinstance(value, datetime):
            return utc(value)
    return None


def encode(payload: Any) -> dict[str, Any]:
    """Serialise one feed event. Decimals stay strings so nothing is lost to floats."""
    if isinstance(payload, Candle):
        return {
            "kind": "bar",
            "symbol": payload.symbol,
            "timeframe": payload.timeframe,
            "ts": iso(payload.ts),
            "open": str(payload.open),
            "high": str(payload.high),
            "low": str(payload.low),
            "close": str(payload.close),
            "volume": str(payload.volume),
        }
    if isinstance(payload, Trade):
        return {
            "kind": "tick",
            "symbol": payload.symbol,
            "ts": iso(payload.ts),
            "price": str(payload.price),
            "qty": str(payload.qty),
            "taker_side": payload.taker_side.value,
        }
    if isinstance(payload, OrderBook):
        return {
            "kind": "book",
            "symbol": payload.symbol,
            "ts": iso(payload.ts),
            "bids": [[str(level.price), str(level.qty)] for level in payload.bids],
            "asks": [[str(level.price), str(level.qty)] for level in payload.asks],
        }
    raise TypeError(f"cannot record {type(payload).__name__}")


def decode(row: dict[str, Any]) -> tuple[str, Any]:
    """Inverse of :func:`encode`; returns ``(engine_kind, payload)``."""
    from ..core.models import BookLevel

    kind = row["kind"]
    ts = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
    if kind == "bar":
        return "bar", Candle(
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            ts=ts,
            open=D(row["open"]),
            high=D(row["high"]),
            low=D(row["low"]),
            close=D(row["close"]),
            volume=D(row["volume"]),
        )
    if kind == "tick":
        return "tick", Trade(
            symbol=row["symbol"],
            ts=ts,
            price=D(row["price"]),
            qty=D(row["qty"]),
            taker_side=Side(row["taker_side"]),
        )
    if kind == "book":
        return "book", OrderBook(
            symbol=row["symbol"],
            ts=ts,
            bids=tuple(BookLevel(price=D(p), qty=D(q)) for p, q in row["bids"]),
            asks=tuple(BookLevel(price=D(p), qty=D(q)) for p, q in row["asks"]),
        )
    raise ValueError(f"unknown tape row kind {kind!r}")


@dataclass
class Tape:
    """An ordered recording of one session's feed.

    Each entry keeps the clock value that was current when the event was emitted. A live
    feed advances time *before* it emits (the minute's ticks arrive with the clock already
    at the bar's close), and that trajectory is part of the session: without it a replay
    sees every event slightly early, order timeouts fire at different moments and fills
    drift apart.
    """

    events: list[tuple[str, Any, datetime | None]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def append(self, kind: str, payload: Any, clock_now: datetime | None = None) -> None:
        if kind in TAPE_KINDS:
            self.events.append((kind, payload, clock_now))

    def __len__(self) -> int:
        return len(self.events)

    @property
    def first_ts(self) -> datetime | None:
        for _kind, payload, stamp in self.events:
            value = stamp or _event_ts(payload)
            if value is not None:
                return value
        return None

    @property
    def last_ts(self) -> datetime | None:
        for _kind, payload, stamp in reversed(self.events):
            value = stamp or _event_ts(payload)
            if value is not None:
                return value
        return None

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for kind, _, _stamp in self.events:
            out[kind] = out.get(kind, 0) + 1
        return out

    def summary(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "events": len(self.events),
            "bars": counts.get("bar", 0),
            "ticks": counts.get("tick", 0),
            "books": counts.get("book", 0),
            "start": iso(self.first_ts) if self.first_ts else None,
            "end": iso(self.last_ts) if self.last_ts else None,
            **self.meta,
        }

    # --- persistence ---------------------------------------------------------
    def write(self, path: str | Path) -> Path:
        """One JSON object per line: greppable, appendable, and cheap to stream."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "meta", **self.meta}) + "\n")
            for _, payload, clock_now in self.events:
                row = encode(payload)
                if clock_now is not None:
                    row["clock"] = iso(clock_now)
                handle.write(json.dumps(row) + "\n")
        return out

    @classmethod
    def read(cls, path: str | Path) -> Tape:
        tape = cls()
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if row.get("kind") == "meta":
                    tape.meta = {k: v for k, v in row.items() if k != "kind"}
                    continue
                clock_now = None
                if row.get("clock"):
                    clock_now = datetime.fromisoformat(str(row["clock"]).replace("Z", "+00:00"))
                kind, payload = decode(row)
                tape.events.append((kind, payload, clock_now))
        return tape


class RecordingFeed(BaseFeed):
    """Forwards a feed and keeps a copy of everything it emitted."""

    name = "recording"

    def __init__(self, config: Config, inner: BaseFeed, clock: Clock | None = None) -> None:
        super().__init__(config, clock or getattr(inner, "clock", None))
        self.inner = inner
        self.tape = Tape()
        self.name = f"recording({getattr(inner, 'name', 'feed')})"

    # delegate the feed's contract to the wrapped feed
    @property
    def symbols(self) -> list[str]:
        return self.inner.symbols

    @property
    def base_timeframe(self) -> str:
        return self.inner.base_timeframe

    def market_rules(self) -> dict[str, MarketRules]:
        return self.inner.market_rules()

    @property
    def running(self) -> bool:
        return self.inner.running

    @property
    def rng(self) -> Any:  # the synthetic feed's generator, so seeding still works
        return getattr(self.inner, "rng", None)

    def stop(self) -> None:
        self.inner.stop()

    def describe(self) -> dict[str, Any]:
        return {**self.inner.describe(), "recording": True}

    async def run(self, emit: Emit) -> None:
        async def record(kind: str, payload: Any) -> None:
            self.tape.append(kind, payload, self.clock.now() if hasattr(self.clock, "now") else None)
            await emit(kind, payload)

        await self.inner.run(record)


class TapeFeed(BaseFeed):
    """Replays a recorded tape through the engine, at whatever speed asked for."""

    name = "tape"

    def __init__(
        self,
        config: Config,
        tape: Tape,
        clock: Clock | None = None,
        *,
        pace: float = 0.0,
    ) -> None:
        super().__init__(config, clock)
        self.tape = tape
        self.pace = pace
        self.emitted = 0
        self._symbols = sorted(
            {payload.symbol for _, payload, _stamp in tape.events if hasattr(payload, "symbol")}
        )
        self._rules = self._config_rules()

    @property
    def symbols(self) -> list[str]:
        return self._symbols or super().symbols

    @property
    def base_timeframe(self) -> str:
        for kind, payload, _stamp in self.tape.events:
            if kind == "bar":
                return payload.timeframe
        return super().base_timeframe

    def market_rules(self) -> dict[str, MarketRules]:
        rules: dict[str, MarketRules] = dict(self._rules)
        # A recorded session can name a symbol the current config no longer lists; the
        # replay must still be able to price it, so borrow the nearest rule set.
        if rules:
            for symbol in self.symbols:
                rules.setdefault(symbol, next(iter(rules.values())))
        return rules

    async def run(self, emit: Emit) -> None:
        self._running = True
        previous: datetime | None = None
        try:
            for kind, payload, recorded_clock in self.tape.events:
                if not self._running:
                    break
                stamp = recorded_clock or _event_ts(payload)
                if stamp is not None:
                    if hasattr(self.clock, "set"):
                        self.clock.set(stamp)
                    if self.pace > 0 and previous is not None:
                        gap = (stamp - previous).total_seconds() / self.pace
                        if gap > 0:
                            import asyncio

                            await asyncio.sleep(min(gap, 30.0))
                    previous = stamp
                await emit(kind, payload)
                self.emitted += 1
        finally:
            self._running = False


def summarise_events(events: Iterable[tuple[str, Any, datetime | None]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind, _, _stamp in events:
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def bars_from_tape(tape: Tape, timeframe: str) -> dict[str, list[Candle]]:
    """Extract just the base bars, for the cheaper candle-only replay path."""
    out: dict[str, list[Candle]] = {}
    for kind, payload, _stamp in tape.events:
        if kind == "bar" and payload.timeframe == timeframe:
            out.setdefault(payload.symbol, []).append(payload)
    return out


def tape_size_hint(tape: Tape) -> dict[str, float]:
    """Rough on-disk size, so retention can be set from a measurement (§18.1)."""
    bars = ticks = books = 0
    levels = 0
    for kind, payload, _stamp in tape.events:
        if kind == "bar":
            bars += 1
        elif kind == "tick":
            ticks += 1
        elif kind == "book":
            books += 1
            levels += len(payload.bids) + len(payload.asks)
    # ~120 bytes per bar, ~60 per tick, ~70 per book level
    estimate = bars * 120 + ticks * 60 + levels * 70
    return {
        "bytes_estimate": float(estimate),
        "megabytes_estimate": round(estimate / 1e6, 3),
        "book_levels": float(levels),
    }


def decimal_or_none(value: Any) -> Decimal | None:
    return D(str(value)) if value is not None else None


def order_by_time(
    events: Sequence[tuple[str, Any, datetime | None]]
) -> list[tuple[str, Any, datetime | None]]:
    return sorted(events, key=lambda item: item[2] or _event_ts(item[1]) or datetime.min)
