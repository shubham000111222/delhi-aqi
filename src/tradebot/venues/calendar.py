"""Market calendars: when a venue is open, and what "before the close" means.

Crypto is always open, so the engine never had to ask. An equity venue does have to be
asked: entries outside a session are invalid, and an intraday position must be closed
before the bell. Both questions are answered here, in exchange local time, so the rest of
the system can stay in UTC.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from ..core.clock import utc


@dataclass(frozen=True)
class Session:
    """One trading day's windows, in the venue's local timezone."""

    trading_date: date
    pre_open: tuple[time, time] | None
    open: time
    close: time

    def bounds(self, tz: tzinfo) -> tuple[datetime, datetime]:
        return (
            datetime.combine(self.trading_date, self.open, tzinfo=tz),
            datetime.combine(self.trading_date, self.close, tzinfo=tz),
        )


class MarketCalendar(ABC):
    """The venue's clock contract."""

    name = "calendar"
    tz: tzinfo

    @abstractmethod
    def is_open(self, ts: datetime) -> bool:
        """True when orders may be matched right now."""

    @abstractmethod
    def is_trading_day(self, day: date) -> bool: ...

    @abstractmethod
    def session_for(self, day: date) -> Session | None: ...

    @abstractmethod
    def next_open(self, ts: datetime) -> datetime: ...

    def next_close(self, ts: datetime) -> datetime | None:
        """The close of the session containing ``ts`` (None when closed all day)."""
        local = utc(ts).astimezone(self.tz)
        session = self.session_for(local.date())
        if session is None:
            return None
        _, close = session.bounds(self.tz)
        return close

    def seconds_to_close(self, ts: datetime) -> float | None:
        close = self.next_close(ts)
        if close is None:
            return None
        return (close - utc(ts)).total_seconds()


class AlwaysOpenCalendar(MarketCalendar):
    """Crypto: 24/7, no holidays, no pre-open."""

    name = "always-open"

    @property
    def tz(self) -> tzinfo:  # type: ignore[override]
        return _utc()

    def is_open(self, ts: datetime) -> bool:  # noqa: ARG002 - always true
        return True

    def is_trading_day(self, day: date) -> bool:  # noqa: ARG002
        return True

    def session_for(self, day: date) -> Session:
        return Session(day, None, time(0, 0), time(23, 59, 59))

    def next_open(self, ts: datetime) -> datetime:
        return utc(ts)

    def next_close(self, ts: datetime) -> None:
        return None


class NSECalendar(MarketCalendar):
    """NSE equities: 09:15-15:30 IST, pre-open 09:00-09:15, weekdays, holiday list.

    The holiday list is *data*, not logic: pass the exchange's published list for the year
    you are trading. An empty list is a weekday-only calendar, which is exactly what you
    want in a test and never what you want in production.
    """

    name = "NSE"

    @property
    def tz(self) -> tzinfo:  # type: ignore[override]
        # resolved lazily: the helper is defined below the class
        return _ist()

    def __init__(
        self,
        *,
        holidays: Iterable[date] = (),
        open_time: time = time(9, 15),
        close_time: time = time(15, 30),
        pre_open: tuple[time, time] | None = (time(9, 0), time(9, 15)),
    ) -> None:
        self.holidays = frozenset(holidays)
        self.open_time = open_time
        self.close_time = close_time
        self.pre_open = pre_open

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def session_for(self, day: date) -> Session | None:
        if not self.is_trading_day(day):
            return None
        return Session(day, self.pre_open, self.open_time, self.close_time)

    def is_open(self, ts: datetime) -> bool:
        local = utc(ts).astimezone(self.tz)
        session = self.session_for(local.date())
        if session is None:
            return False
        opened, closed = session.bounds(self.tz)
        return opened <= local < closed

    def next_open(self, ts: datetime) -> datetime:
        local = utc(ts).astimezone(self.tz)
        for offset in range(0, 400):  # a year of holidays cannot hide the next session
            day = local.date() + timedelta(days=offset)
            session = self.session_for(day)
            if session is None:
                continue
            opened, _ = session.bounds(self.tz)
            if opened > local:
                return opened.astimezone(_utc())
        raise RuntimeError("no trading session found in the next 400 days")


def _utc() -> tzinfo:
    return UTC


def _ist() -> tzinfo:
    """Asia/Kolkata without a tzdata dependency (IST has no DST)."""
    return timezone_fixed(5, 30)


def timezone_fixed(hours: int, minutes: int) -> tzinfo:
    from datetime import timezone

    return timezone(timedelta(hours=hours, minutes=minutes))
