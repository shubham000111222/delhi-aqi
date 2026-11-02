"""Cost models per venue: what a round trip actually costs.

Crypto is simple enough to hard-code: a percentage of notional per side. An Indian
equity trade is a stack of charges, and which ones apply depends on the side and the
product — brokerage, STT, exchange transaction charges, GST on the service components,
stamp duty on the buy, SEBI turnover fee, and DP charges on a delivery sell. Ignoring
any of them makes a backtest look better than the account will.

All rates are configuration, not law: verify them against your broker's tariff sheet and
the current SEBI circulars before trusting a number. The defaults here are the commonly
quoted rates as of writing, rounded conservatively.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..core.money import ZERO, D, to_str


@dataclass(frozen=True)
class CostBreakdown:
    """Itemised charges for one fill, in the instrument's currency."""

    brokerage: Decimal = ZERO
    stt: Decimal = ZERO
    exchange: Decimal = ZERO
    sebi: Decimal = ZERO
    stamp: Decimal = ZERO
    gst: Decimal = ZERO
    dp: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst + self.dp

    def as_dict(self) -> dict[str, str]:
        return {
            "brokerage": to_str(self.brokerage, 4),
            "stt": to_str(self.stt, 4),
            "exchange": to_str(self.exchange, 4),
            "sebi": to_str(self.sebi, 4),
            "stamp": to_str(self.stamp, 4),
            "gst": to_str(self.gst, 4),
            "dp": to_str(self.dp, 4),
            "total": to_str(self.total, 4),
        }

    def __add__(self, other: CostBreakdown) -> CostBreakdown:
        return CostBreakdown(
            brokerage=self.brokerage + other.brokerage,
            stt=self.stt + other.stt,
            exchange=self.exchange + other.exchange,
            sebi=self.sebi + other.sebi,
            stamp=self.stamp + other.stamp,
            gst=self.gst + other.gst,
            dp=self.dp + other.dp,
        )


class CostModel(ABC):
    """Contract: turn (notional, side, product) into an itemised cost."""

    name = "costs"

    @abstractmethod
    def charges(self, *, notional: Decimal, side: str, product: str = "intraday") -> CostBreakdown: ...


class CryptoCostModel(CostModel):
    """Percentage fee per side, the model the paper adapter already uses."""

    name = "crypto"

    def __init__(self, *, maker_fee: Decimal | str = "0.001", taker_fee: Decimal | str = "0.001") -> None:
        self.maker_fee = D(maker_fee)
        self.taker_fee = D(taker_fee)

    def charges(
        self, *, notional: Decimal, side: str, product: str = "intraday", liquidity: str = "taker"
    ) -> CostBreakdown:
        rate = self.maker_fee if liquidity == "maker" else self.taker_fee
        return CostBreakdown(exchange=notional * rate)


class NSECostModel(CostModel):
    """The Indian equity stack, for intraday and delivery.

    Rates (fractions of notional unless stated) are constructor arguments so a broker
    tariff change is a config change, not a code change.
    """

    name = "nse"

    def __init__(
        self,
        *,
        brokerage_pct: Decimal | str = "0.0003",  # 0.03% or Rs 20, whichever is lower
        brokerage_cap: Decimal | str = "20",
        stt_intraday_sell: Decimal | str = "0.00025",  # 0.025% on the sell side
        stt_delivery: Decimal | str = "0.001",  # 0.1% both sides
        exchange_pct: Decimal | str = "0.0000322",  # NSE transaction charge
        sebi_pct: Decimal | str = "0.000001",  # Rs 10 per crore
        stamp_buy_delivery: Decimal | str = "0.00015",
        stamp_buy_intraday: Decimal | str = "0.00003",
        gst_pct: Decimal | str = "0.18",  # on brokerage + exchange + SEBI
        dp_charge_per_sell: Decimal | str = "15.34",  # delivery sell only
    ) -> None:
        self.brokerage_pct = D(brokerage_pct)
        self.brokerage_cap = D(brokerage_cap)
        self.stt_intraday_sell = D(stt_intraday_sell)
        self.stt_delivery = D(stt_delivery)
        self.exchange_pct = D(exchange_pct)
        self.sebi_pct = D(sebi_pct)
        self.stamp_buy_delivery = D(stamp_buy_delivery)
        self.stamp_buy_intraday = D(stamp_buy_intraday)
        self.gst_pct = D(gst_pct)
        self.dp_charge_per_sell = D(dp_charge_per_sell)

    def charges(self, *, notional: Decimal, side: str, product: str = "intraday") -> CostBreakdown:
        if notional <= 0:
            return CostBreakdown()
        buy = side.lower() in ("buy", "b")
        delivery = product.lower() in ("delivery", "cnc", "longterm")

        brokerage = min(notional * self.brokerage_pct, self.brokerage_cap)
        exchange = notional * self.exchange_pct
        sebi = notional * self.sebi_pct

        if delivery:
            stt = notional * self.stt_delivery
            stamp = notional * self.stamp_buy_delivery if buy else ZERO
            dp = ZERO if buy else self.dp_charge_per_sell
        else:
            stt = ZERO if buy else notional * self.stt_intraday_sell
            stamp = notional * self.stamp_buy_intraday if buy else ZERO
            dp = ZERO

        gst = (brokerage + exchange + sebi) * self.gst_pct
        return CostBreakdown(
            brokerage=brokerage, stt=stt, exchange=exchange, sebi=sebi, stamp=stamp, gst=gst, dp=dp
        )


def round_trip(
    model: CostModel, *, entry_notional: Decimal, exit_notional: Decimal, product: str = "intraday"
) -> CostBreakdown:
    """Cost of a complete trade: buy plus sell."""
    return model.charges(notional=entry_notional, side="buy", product=product) + model.charges(
        notional=exit_notional, side="sell", product=product
    )


def breakeven_move(model: CostModel, *, notional: Decimal, product: str = "intraday") -> Decimal:
    """How far price must move, as a fraction, just to cover the round trip.

    The most useful number a cost model produces: it says whether a strategy's edge is
    even addressable at its holding period.
    """
    if notional <= 0:
        return ZERO
    total = round_trip(model, entry_notional=notional, exit_notional=notional, product=product).total
    return total / notional


def compare_models(notional: Decimal, models: list[CostModel], product: str = "intraday") -> list[dict[str, Any]]:
    """Side-by-side round-trip cost, for the docs and for choosing a venue."""
    out: list[dict[str, Any]] = []
    for model in models:
        breakdown = round_trip(model, entry_notional=notional, exit_notional=notional, product=product)
        out.append(
            {
                "model": model.name,
                "product": product,
                "notional": to_str(notional, 2),
                "total": to_str(breakdown.total, 4),
                "rate": to_str(breakdown.total / notional, 6) if notional else "0",
                **breakdown.as_dict(),
            }
        )
    return out


@dataclass
class CostLedger:
    """Accumulates charges across a session, so reports can show the true cost drag."""

    model: CostModel
    product: str = "intraday"
    by_side: dict[str, Decimal] = field(default_factory=lambda: {"buy": ZERO, "sell": ZERO})

    def record(self, *, notional: Decimal, side: str) -> CostBreakdown:
        breakdown = self.model.charges(notional=notional, side=side, product=self.product)
        key = "buy" if side.lower().startswith("b") else "sell"
        self.by_side[key] = self.by_side.get(key, ZERO) + breakdown.total
        return breakdown

    @property
    def total(self) -> Decimal:
        return sum(self.by_side.values(), ZERO)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.name,
            "product": self.product,
            "buy": to_str(self.by_side.get("buy", ZERO), 2),
            "sell": to_str(self.by_side.get("sell", ZERO), 2),
            "total": to_str(self.total, 2),
        }
