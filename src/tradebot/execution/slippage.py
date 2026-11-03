"""Slippage recording and calibration (§6, §18.1).

Assumed slippage is a guess that flatters a backtest. This module replaces the guess with
a measurement: every fill records what the order expected and what it got, the record is
kept with the size and the state of the book, and the resulting distribution becomes the
model used to size and to stress.

The distinction that matters:

* **Recorder** - passive. It observes fills and computes `slippage_bps` itself rather than
  trusting the adapter's number, because a bug in the adapter would otherwise calibrate
  the model to its own error.
* **Model** - fitted from the record. Slippage is non-linear in size: walking the first
  level of a book is nearly free, and then it costs sharply. So the model is a per-symbol
  table of size buckets (as a fraction of visible depth), reported as percentiles, plus a
  conservative fallback for sizes never seen. It never extrapolates optimistically: an
  unseen bucket returns the worst observed value, not the average.
* **Calibration check** - compares what the model predicted for realised fills against what
  they actually cost. A model that is systematically optimistic is worse than no model.

Nothing here changes trading behaviour by itself. It produces numbers for the backtester,
the daily report and the risk checks.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..core.models import Fill, OrderBook, Side
from ..core.money import D, to_str

#: Size buckets as a fraction of visible top-of-book depth.
BUCKETS = (0.0, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0)


def bucket_for(fraction: float) -> float:
    """The bucket a size falls into: the largest edge that does not exceed it."""
    for edge in reversed(BUCKETS[1:]):
        if fraction >= edge:
            return edge
    return BUCKETS[1]


def implied_bps(fill: Fill) -> float:
    """Slippage in basis points, signed so positive always means *worse for us*.

    Computed from the prices rather than read off the fill, so a wrong adapter number
    cannot calibrate the model to itself.
    """
    expected = fill.expected_price
    if expected is None or expected <= 0 or fill.price <= 0:
        return float(fill.slippage_bps)
    raw = float((fill.price - expected) / expected) * 10_000.0
    # a buy filled above expectation is adverse; a sell filled below expectation is adverse
    return raw if fill.side is Side.BUY else -raw


def depth_at_best(book: OrderBook | None, side: Side, levels: int = 1) -> float:
    """Visible quantity on the side we would trade against, in base units."""
    if book is None:
        return 0.0
    levels_list = book.asks if side is Side.BUY else book.bids
    return sum(float(level.qty) for level in levels_list[:levels])


@dataclass
class SlippageRecord:
    symbol: str
    ts: datetime
    strategy: str
    side: Side
    qty: Decimal
    price: Decimal
    expected_price: Decimal
    bps: float
    depth_fraction: float
    liquidity: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ts": self.ts.isoformat(),
            "strategy": self.strategy,
            "side": self.side.value,
            "qty": to_str(self.qty),
            "price": to_str(self.price),
            "bps": round(self.bps, 4),
            "depth_fraction": round(self.depth_fraction, 4),
            "liquidity": self.liquidity,
        }


@dataclass
class SlippageBucket:
    edge: float
    samples: int
    median_bps: float
    p95_bps: float
    worst_bps: float
    mean_bps: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "size_vs_depth_up_to": self.edge,
            "samples": self.samples,
            "median_bps": round(self.median_bps, 3),
            "p95_bps": round(self.p95_bps, 3),
            "worst_bps": round(self.worst_bps, 3),
            "mean_bps": round(self.mean_bps, 3),
        }


@dataclass
class SlippageModel:
    """Per-symbol, per-bucket slippage table with a conservative fallback."""

    buckets: dict[str, dict[float, SlippageBucket]] = field(default_factory=dict)
    fallback_bps: float = 10.0
    samples: int = 0

    # --- building ------------------------------------------------------
    @classmethod
    def fit(cls, records: list[SlippageRecord]) -> SlippageModel:
        model = cls()
        grouped: dict[tuple[str, float], list[float]] = {}
        for record in records:
            key = (record.symbol, bucket_for(record.depth_fraction))
            grouped.setdefault(key, []).append(record.bps)
        for (symbol, edge), values in grouped.items():
            ordered = sorted(values)
            model.buckets.setdefault(symbol, {})[edge] = SlippageBucket(
                edge=edge,
                samples=len(values),
                median_bps=statistics.median(values),
                p95_bps=_quantile(ordered, 0.95),
                worst_bps=max(values),
                mean_bps=statistics.fmean(values),
            )
        model.samples = len(records)
        if records:
            model.fallback_bps = max(max(r.bps for r in records), 1.0)
        return model

    # --- using ---------------------------------------------------------
    def predict_bps(self, symbol: str, qty: Decimal, book: OrderBook | None, side: Side) -> float:
        """Expected slippage for this order, never better than what has been observed.

        An unseen size bucket returns the symbol's worst bucket rather than a hopeful
        average: the point of a model here is to avoid optimism, not to look precise.
        """
        table = self.buckets.get(symbol)
        if not table:
            return self.fallback_bps
        depth = depth_at_best(book, side)
        fraction = float(qty) / depth if depth > 0 else BUCKETS[-1]
        edge = bucket_for(fraction)
        if edge in table:
            return table[edge].p95_bps
        nearest = max(table)  # the largest bucket we know about
        return table[nearest].worst_bps if edge > nearest else table[nearest].p95_bps

    def expected_bps(self) -> float:
        """A sample-weighted average of what fills have actually cost.

        Distinct from `fallback_bps`, which is the *worst single fill* and exists to keep
        the size cap pessimistic. A cost budget asks a different question - "can this
        target pay for the round trip?" - and answering it with the worst fill on record
        would refuse almost everything. This is the number to compare a target against;
        the worst is the number to size against.
        """
        weighted = 0.0
        weight = 0
        for buckets in self.buckets.values():
            for bucket in buckets.values():
                weighted += bucket.mean_bps * bucket.samples
                weight += bucket.samples
        if weight == 0:
            return self.fallback_bps if not self.buckets else 0.0
        return weighted / weight

    def table(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for symbol, buckets in sorted(self.buckets.items()):
            for edge in sorted(buckets):
                rows.append({"symbol": symbol} | buckets[edge].as_dict())
        return rows


def _quantile(ordered: list[float], pct: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(pct * (len(ordered) - 1)))))
    return ordered[index]


class SlippageRecorder:
    """Collects fills and can be asked for the distribution, the model, or the errors."""

    def __init__(self, *, max_records: int = 50_000) -> None:
        self.records: list[SlippageRecord] = []
        self.max_records = max_records
        self.skipped = 0

    def observe(
        self,
        fill: Fill,
        *,
        book: OrderBook | None = None,
        qty: Decimal | None = None,
    ) -> SlippageRecord | None:
        if fill.expected_price is None:
            self.skipped += 1
            return None
        qty = qty if qty is not None else fill.qty
        depth = depth_at_best(book, fill.side)
        record = SlippageRecord(
            symbol=fill.symbol,
            ts=fill.ts,
            strategy=fill.strategy,
            side=fill.side,
            qty=fill.qty,
            price=fill.price,
            expected_price=fill.expected_price,
            bps=implied_bps(fill),
            depth_fraction=(float(qty) / depth) if depth > 0 else math.inf,
            liquidity=fill.liquidity,
        )
        if len(self.records) >= self.max_records:
            self.records.pop(0)
        self.records.append(record)
        return record

    # --- reporting ------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        if not self.records:
            return {"fills": 0, "skipped": self.skipped, "note": "no fills with an expected price"}
        values = sorted(r.bps for r in self.records)
        by_symbol: dict[str, list[float]] = {}
        for record in self.records:
            by_symbol.setdefault(record.symbol, []).append(record.bps)
        return {
            "fills": len(self.records),
            "skipped": self.skipped,
            "median_bps": round(statistics.median(values), 3),
            "mean_bps": round(statistics.fmean(values), 3),
            "p95_bps": round(_quantile(values, 0.95), 3),
            "worst_bps": round(max(values), 3),
            "best_bps": round(min(values), 3),
            "adverse_share": round(sum(1 for v in values if v > 0) / len(values), 3),
            "by_symbol": {
                symbol: {
                    "fills": len(items),
                    "median_bps": round(statistics.median(items), 3),
                    "p95_bps": round(_quantile(sorted(items), 0.95), 3),
                }
                for symbol, items in sorted(by_symbol.items())
            },
        }

    def model(self) -> SlippageModel:
        return SlippageModel.fit(self.records)

    def calibration(self, model: SlippageModel | None = None) -> dict[str, Any]:
        """Did the model over- or under-estimate what actually happened?"""
        model = model or self.model()
        errors: list[float] = []
        for record in self.records:
            predicted = model.predict_bps(record.symbol, record.qty, None, record.side)
            errors.append(predicted - record.bps)  # positive = model too pessimistic
        if not errors:
            return {"samples": 0}
        mean_error = statistics.fmean(errors)
        optimistic_share = sum(1 for e in errors if e < 0) / len(errors)
        return {
            "samples": len(errors),
            "mean_error_bps": round(mean_error, 3),
            "optimistic_share": round(optimistic_share, 3),
            "verdict": (
                "model is on the optimistic side for "
                f"{optimistic_share:.0%} of fills - widen the assumption"
                if optimistic_share > 0.6
                else "model is not systematically optimistic"
            ),
        }

    def as_rows(self) -> list[dict[str, Any]]:
        return [record.as_dict() for record in self.records]


def calibrate_from_fills(fills: list[Fill], *, books: dict[str, OrderBook] | None = None) -> tuple[SlippageModel, dict[str, Any]]:
    """Convenience: recorder over a finished run, plus its summary."""
    recorder = SlippageRecorder()
    books = books or {}
    for fill in fills:
        recorder.observe(fill, book=books.get(fill.symbol))
    return recorder.model(), recorder.summary()


def size_for_budget(
    model: SlippageModel,
    symbol: str,
    book: OrderBook,
    side: Side,
    *,
    budget_bps: float,
    max_qty: Decimal | None = None,
) -> Decimal:
    """Largest quantity whose *predicted* slippage stays inside ``budget_bps``.

    Used by sizing to avoid the trade that is too big for the book. When even the smallest
    bucket breaches the budget, the answer is zero - a deliberate refusal rather than a
    small order that will quietly pay far more than the edge.
    """
    depth = depth_at_best(book, side)
    if depth <= 0:
        return D("0")
    table = model.buckets.get(symbol)
    if not table:
        return D("0") if model.fallback_bps > budget_bps else D(str(depth))
    affordable = 0.0
    for edge in sorted(table):
        if table[edge].p95_bps <= budget_bps:
            affordable = min(edge, BUCKETS[-1]) * depth
        else:
            break
    if max_qty is not None:
        affordable = min(affordable, float(max_qty))
    return D(str(round(affordable, 8)))
