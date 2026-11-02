"""Venue abstractions: calendars, instruments, cost models, sessions and square-off.

Crypto never closes and charges a percentage fee, which is why a single global config
was enough until now. An Indian equity venue is different in four ways that reach into
the trading logic: it has sessions, it has instruments with tick/lot/band rules, it has
a cost stack (brokerage, STT, exchange, GST, stamp, SEBI), and intraday positions must be
squared off before the close. These modules are the seams for that, so adding NSE later
is an adapter job rather than a rewrite (build plan §18.8).
"""

from .calendar import AlwaysOpenCalendar, MarketCalendar, NSECalendar, Session
from .costs import (
    CostBreakdown,
    CostLedger,
    CostModel,
    CryptoCostModel,
    NSECostModel,
    breakeven_move,
    compare_models,
    round_trip,
)
from .instruments import Instrument, Product
from .session import SessionManager, StaticSession
from .squareoff import SquareOffScheduler

__all__ = [
    "AlwaysOpenCalendar",
    "CostBreakdown",
    "CostLedger",
    "CostModel",
    "CryptoCostModel",
    "Instrument",
    "MarketCalendar",
    "NSECalendar",
    "NSECostModel",
    "Product",
    "Session",
    "SessionManager",
    "SquareOffScheduler",
    "StaticSession",
    "breakeven_move",
    "compare_models",
    "round_trip",
]
