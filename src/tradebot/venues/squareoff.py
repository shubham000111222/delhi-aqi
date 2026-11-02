"""Mandatory square-off: close intraday positions before the bell, unconditionally.

A broker squares off intraday positions itself a few minutes before the close, at market
and with a penalty. The bot must never let that happen: it flattens on its own terms,
earlier, so the exits are the ones the ledger modelled.

The scheduler works off the venue calendar, in market time — so it behaves identically in
a backtest, a paced demo and live trading, which is exactly why it lives here and not in
a wall-clock timer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..core.clock import utc
from .calendar import MarketCalendar
from .instruments import Product


@dataclass
class SquareOffPlan:
    """What should happen, and when, for one session."""

    session_close: datetime
    deadline: datetime  # the moment the bot starts flattening
    due_now: bool
    seconds_left: float
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "session_close": self.session_close.isoformat(),
            "deadline": self.deadline.isoformat(),
            "due": self.due_now,
            "seconds_left": round(self.seconds_left, 1),
            "reason": self.reason,
        }


class SquareOffScheduler:
    """Decides when to flatten intraday exposure.

    ``lead_minutes`` is the safety margin before the close (default 20 min: later than the
    broker's own square-off window is too late). Delivery positions are never flattened —
    that would be a tax event and is not the bot's business.
    """

    def __init__(
        self,
        calendar: MarketCalendar,
        *,
        lead_minutes: float = 20.0,
        product: Product = Product.INTRADAY,
    ) -> None:
        self.calendar = calendar
        self.lead = timedelta(minutes=lead_minutes)
        self.product = product

    def plan(self, now: datetime) -> SquareOffPlan | None:
        """The plan for the session ``now`` falls in, or None when the venue is shut."""
        now = utc(now)
        close = self.calendar.next_close(now)
        if close is None:
            # always-open venues have no close, so no square-off
            return None
        deadline = close - self.lead
        seconds_left = (deadline - now).total_seconds()
        return SquareOffPlan(
            session_close=close,
            deadline=deadline,
            due_now=seconds_left <= 0 and now < close,
            seconds_left=seconds_left,
            reason=(
                "inside the square-off window"
                if seconds_left <= 0
                else f"square-off at {deadline.isoformat()}"
            ),
        )

    def should_flatten(self, now: datetime) -> bool:
        """True once we are inside the window and the venue is still open."""
        plan = self.plan(now)
        return bool(plan and plan.due_now)

    def missed(self, now: datetime) -> bool:
        """True if the close passed with intraday positions still marked open.

        That is an incident, not a normal path: the broker has already flattened them and
        charged for it, so the ledger disagrees with the venue until reconciliation.
        """
        now = utc(now)
        close = self.calendar.next_close(now)
        if close is None:
            return False
        return now >= close and self.calendar.is_trading_day(now.date())

    def next_session_deadline(self, now: datetime) -> datetime | None:
        """Deadline for the *next* session, for scheduling and alerting."""
        now = utc(now)
        close = self.calendar.next_close(now)
        if close is not None and close > now:
            return close - self.lead
        upcoming = self.calendar.next_open(now)
        close = self.calendar.next_close(upcoming)
        return close - self.lead if close else None
