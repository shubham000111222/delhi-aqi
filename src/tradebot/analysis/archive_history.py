"""Bounded point-in-time recorded-history provider for the research engine gate.

No synthetic fallback, resampling, deduplication or forward filling. Archive rows
are observations, not attested venue truth; TimeMachine independently validates
contiguity, staleness, values and minimum coverage before computing outcomes.
"""
from datetime import datetime, timedelta
from decimal import Decimal

from ..core.clock import parse_iso, utc
from ..core.models import Candle
from ..data.archive import Archive


class ArchiveHistoryProvider:
    def __init__(self, archive: Archive, *, days: int = 60):
        if isinstance(days, bool) or not isinstance(days, int) or not 30 <= days <= 180:
            raise ValueError('recorded-history window must be 30..180 days')
        self.archive = archive
        self.days = days

    def __call__(self, symbol: str, decision_time: datetime) -> tuple[Candle, ...]:
        if decision_time.tzinfo is None or decision_time.utcoffset() is None:
            raise ValueError('recorded-history cutoff must be timezone aware')
        end = utc(decision_time) - timedelta(minutes=5)
        start = utc(decision_time) - timedelta(days=self.days)
        if not any(self.archive.root.glob('candles/**/*.parquet')):
            raise ValueError('no recorded candle archive; synthetic fallback forbidden')
        # One extra row detects duplicate/oversized windows without silently trimming.
        maximum = self.days * 288
        rows = self.archive.query(
            "SELECT symbol,timeframe,ts,open,high,low,close,volume FROM candles "
            "WHERE symbol=? AND timeframe='5m' AND ts>=? AND ts<=? ORDER BY ts LIMIT ?",
            [symbol, start, end, maximum + 1])
        if len(rows) > maximum:
            raise ValueError('recorded-history window exceeds expected 5m row bound')
        return tuple(Candle(str(row['symbol']), str(row['timeframe']), parse_iso(str(row['ts'])),
                            *(Decimal(str(row[key])) for key in ('open', 'high', 'low', 'close', 'volume')))
                     for row in rows)
