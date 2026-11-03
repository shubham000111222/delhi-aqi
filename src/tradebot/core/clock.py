"""Clock abstraction. UTC everywhere; simulated time for backtests and replay."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def utc(ts: datetime) -> datetime:
    """Force a datetime to be timezone-aware UTC."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def iso(ts: datetime) -> str:
    return utc(ts).isoformat().replace("+00:00", "Z")


def parse_iso(text: str) -> datetime:
    return utc(datetime.fromisoformat(text.replace("Z", "+00:00")))


def floor_ts(ts: datetime, seconds: int) -> datetime:
    """Floor a timestamp to a bar boundary."""
    epoch = int(utc(ts).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, UTC)


class Clock:
    """Real wall clock."""

    def now(self) -> datetime:
        return utcnow()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class SimClock(Clock):
    """Simulated clock: `sleep` advances virtual time instead of waiting.

    Backtests and replays never block, and the engine code is unchanged:
    the paper adapter's 100-500 ms latency still lands on a later tick.

    ``pace`` is simulated seconds per real second and is only used by the dashboard
    demo: 0 (the default) means "never wait", which is what a backtest wants, while
    pace=600 means an hour of market per minute of wall time. Even when paced, the
    engine still measures every timeout, cooldown and day boundary on simulated time.
    """

    def __init__(self, start: datetime | None = None, pace: float = 0.0) -> None:
        self._now = utc(start or utcnow())
        self._mono = 0.0
        self.pace = float(pace)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float | timedelta) -> datetime:
        delta = seconds if isinstance(seconds, timedelta) else timedelta(seconds=seconds)
        self._now = self._now + delta
        self._mono += delta.total_seconds()
        return self._now

    def set(self, ts: datetime) -> None:
        """Jump forward to an event timestamp (never backwards)."""
        ts = utc(ts)
        if ts > self._now:
            self._mono += (ts - self._now).total_seconds()
            self._now = ts

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        if self.pace > 0 and seconds > 0:
            await asyncio.sleep(seconds / self.pace)


TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def timeframe_seconds(tf: str) -> int:
    try:
        return TIMEFRAME_SECONDS[tf]
    except KeyError as exc:  # pragma: no cover - config validation covers this
        raise ValueError(f"unsupported timeframe {tf!r}") from exc
