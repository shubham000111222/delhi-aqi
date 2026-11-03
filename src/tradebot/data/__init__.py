"""Market data layer: series, hub (aggregation + quality guards) and feeds.

Only three things live here, and each has one job:

* :mod:`.series` - an ordered, bounded candle series with aligned array views.
* :mod:`.hub` - aggregation to 15m/1h, spike quarantine, staleness and books.
* :mod:`.feed` - where events come from (seeded synthetic market, replay, ccxt live).

The engine sees only the ``emit(kind, payload)`` contract and the hub, so a feed
swap (synthetic -> replay -> live) never touches strategy, risk or execution code.
"""

from __future__ import annotations

from .feed import BaseFeed, CcxtFeed, ReplayFeed, SyntheticFeed, make_feed
from .hub import NEVER_RECEIVED_SECONDS, MarketData, TimeframeAggregator
from .series import CandleSeries

__all__ = [
    "NEVER_RECEIVED_SECONDS",
    "BaseFeed",
    "CandleSeries",
    "CcxtFeed",
    "MarketData",
    "ReplayFeed",
    "SyntheticFeed",
    "TimeframeAggregator",
    "make_feed",
]
