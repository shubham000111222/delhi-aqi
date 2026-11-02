"""A bounded, ordered series of candles with array views for the indicators.

The hub keeps one :class:`CandleSeries` per (symbol, timeframe). Three properties
matter for the rest of the system:

* **Order is enforced.** A bar is accepted only if its timestamp is strictly newer
  than the last one. A duplicate or an out-of-order bar is refused (``False``) and
  counted by the hub; it never silently overwrites history.
* **Memory is bounded.** The ring buffer keeps the newest ``maxlen`` bars. Every
  array view has the same length and the same ordering, so an indicator cannot be
  computed on misaligned data.
* **Arrays are plain numpy.** Strategies and indicators never see pandas types,
  which is what keeps a backtest and live producing identical numbers.

Array views (``open``/``high``/``low``/``close``/``volume``) are rebuilt lazily and
only when a bar is appended, so calling them from several strategies per bar is
cheap.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np

from ..core.clock import iso, utc
from ..core.models import Candle
from ..core.money import ZERO

#: default window: ~26 hours of 1m bars, or ~10 days of 15m bars
DEFAULT_MAXLEN = 1500

_ARRAY_FIELDS = ("open", "high", "low", "close", "volume")


class CandleSeries(Sequence[Candle]):
    """Ordered, bounded candles for one symbol and one timeframe."""

    __slots__ = ("symbol", "timeframe", "maxlen", "_bars", "_arrays", "_timestamps", "_dirty")

    def __init__(self, symbol: str, timeframe: str, maxlen: int = DEFAULT_MAXLEN) -> None:
        if maxlen < 1:
            raise ValueError("maxlen must be at least 1")
        self.symbol = symbol
        self.timeframe = timeframe
        self.maxlen = int(maxlen)
        self._bars: deque[Candle] = deque(maxlen=self.maxlen)
        self._arrays: dict[str, np.ndarray] = {}
        self._timestamps: list[datetime] = []
        self._dirty = True

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------
    def append(self, candle: Candle) -> bool:
        """Append a closed bar. Returns False for a duplicate or older bar."""
        if candle.symbol != self.symbol:
            raise ValueError(f"{self.series_name} cannot hold a {candle.symbol} bar")
        if candle.timeframe != self.timeframe:
            raise ValueError(
                f"{self.series_name} cannot hold a {candle.timeframe} bar"
            )
        ts = utc(candle.ts)
        if self._bars:
            last = utc(self._bars[-1].ts)
            if ts == last or ts < last:
                return False
        self._bars.append(
            Candle(
                symbol=candle.symbol,
                timeframe=candle.timeframe,
                ts=ts,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
            )
        )
        self._dirty = True
        return True

    def clear(self) -> None:
        self._bars.clear()
        self._dirty = True

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._bars)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self._bars)[index]
        return self._bars[index]

    def __iter__(self) -> Iterator[Candle]:
        return iter(self._bars)

    def __bool__(self) -> bool:
        return bool(self._bars)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CandleSeries {self.symbol} {self.timeframe} n={len(self._bars)}>"

    @property
    def series_name(self) -> str:
        return f"{self.symbol} {self.timeframe} series"

    def _refresh(self) -> None:
        if not self._dirty:
            return
        bars = list(self._bars)
        for field in _ARRAY_FIELDS:
            self._arrays[field] = np.array([float(getattr(b, field)) for b in bars], dtype=np.float64)
        self._timestamps = [b.ts for b in bars]
        self._dirty = False

    @property
    def open(self) -> np.ndarray:
        self._refresh()
        return self._arrays["open"]

    @property
    def high(self) -> np.ndarray:
        self._refresh()
        return self._arrays["high"]

    @property
    def low(self) -> np.ndarray:
        self._refresh()
        return self._arrays["low"]

    @property
    def close(self) -> np.ndarray:
        self._refresh()
        return self._arrays["close"]

    @property
    def volume(self) -> np.ndarray:
        self._refresh()
        return self._arrays["volume"]

    @property
    def ts(self) -> list[datetime]:
        self._refresh()
        return self._timestamps

    @property
    def bars(self) -> list[Candle]:
        return list(self._bars)

    @property
    def last_close(self) -> Decimal:
        return self._bars[-1].close if self._bars else ZERO

    @property
    def last_bar(self) -> Candle | None:
        return self._bars[-1] if self._bars else None

    @property
    def first_ts(self) -> datetime | None:
        return self._bars[0].ts if self._bars else None

    @property
    def last_ts(self) -> datetime | None:
        return self._bars[-1].ts if self._bars else None

    def last(self, n: int = 1) -> list[Candle]:
        """The newest ``n`` bars, oldest first."""
        if n <= 0:
            return []
        return list(self._bars)[-n:]

    def since(self, ts: datetime) -> list[Candle]:
        cutoff = utc(ts)
        return [b for b in self._bars if utc(b.ts) >= cutoff]

    # ------------------------------------------------------------------
    # export
    # ------------------------------------------------------------------
    def to_rows(self) -> list[dict[str, Any]]:
        """Plain-python rows (floats, ISO timestamps) for CSV / parquet writers."""
        return [
            {
                "symbol": b.symbol,
                "timeframe": b.timeframe,
                "ts": iso(b.ts),
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume),
            }
            for b in self._bars
        ]

    def to_dataframe(self):
        """A pandas DataFrame when pandas is installed, else a list-like fallback.

        pandas is deliberately not a dependency of this package (indicators work on
        numpy), so analysis outside the bot must not make the bot importable-but-broken.
        The fallback keeps ``len()``, iteration and row access working.
        """
        rows = self.to_rows()
        try:  # pragma: no cover - pandas is an optional analyst tool, not a bot dependency
            import pandas as pd

            frame = pd.DataFrame(rows)
            if not frame.empty:
                frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
            return frame
        except ImportError:
            return RowsFrame(rows)


class RowsFrame:
    """Minimal stand-in for a DataFrame when pandas is unavailable."""

    __slots__ = ("rows",)

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.rows)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.rows[index]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RowsFrame n={len(self.rows)} (install pandas for a real DataFrame)>"


__all__ = ["DEFAULT_MAXLEN", "CandleSeries", "RowsFrame"]
