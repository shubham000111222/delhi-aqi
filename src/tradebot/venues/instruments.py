"""Instrument rules: tick size, lot step, price bands and product type.

Crypto has no daily price band and lot steps are usually 1e-8, so a float price and a
rounded quantity were enough. An equity instrument rejects an order for reasons that have
nothing to do with the strategy: the tick is 5 paise, the quantity must be a multiple of
the lot, and the price cannot sit outside the day's circuit band. Validating that in one
place keeps the broker's rejections out of the trade log.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum

from ..core.money import ZERO, D


class Product(StrEnum):
    """How the position is held, which changes margin, taxes and square-off."""

    INTRADAY = "intraday"  # MIS: must be squared off before the close
    DELIVERY = "delivery"  # CNC: settled T+1, no square-off
    NORMAL = "normal"  # carry-forward (where permitted)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    tick_size: Decimal = D("0.05")
    lot_size: Decimal = D("1")
    min_qty: Decimal = D("1")
    max_qty: Decimal | None = None
    price_band_pct: Decimal | None = D("0.20")  # +/-20% circuit, None = no band
    product: Product = Product.INTRADAY
    freeze_qty: Decimal | None = None

    # --- rounding ---------------------------------------------------------
    def round_price(self, price: Decimal) -> Decimal:
        """Snap to the tick. Half-up: a limit order is never silently made worse."""
        if self.tick_size <= 0:
            return price
        return (price / self.tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * self.tick_size

    def round_qty(self, qty: Decimal) -> Decimal:
        """Snap down to the lot step: never round *up* into more risk than intended."""
        step = self.lot_size if self.lot_size > 0 else D("1")
        return (qty / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step

    # --- validation -------------------------------------------------------
    def band(self, reference_price: Decimal) -> tuple[Decimal, Decimal]:
        """Allowed price range for the day, from a reference (previous close)."""
        if self.price_band_pct is None or reference_price <= 0:
            return ZERO, D("1e18")
        low = reference_price * (D("1") - self.price_band_pct)
        high = reference_price * (D("1") + self.price_band_pct)
        return self.round_price(low), self.round_price(high)

    def validate(self, *, qty: Decimal, price: Decimal | None = None, reference_price: Decimal | None = None) -> list[str]:
        """Return every reason the venue would refuse this order."""
        problems: list[str] = []
        if qty <= 0:
            problems.append("quantity must be positive")
            return problems
        if qty != self.round_qty(qty):
            problems.append(f"quantity {qty} is not a multiple of the lot {self.lot_size}")
        if qty < self.min_qty:
            problems.append(f"quantity {qty} is below the minimum {self.min_qty}")
        if self.max_qty is not None and qty > self.max_qty:
            problems.append(f"quantity {qty} is above the maximum {self.max_qty}")
        if price is not None:
            if price <= 0:
                problems.append("price must be positive")
            elif price != self.round_price(price):
                problems.append(f"price {price} is not a multiple of the tick {self.tick_size}")
            if reference_price is not None and self.price_band_pct is not None:
                low, high = self.band(reference_price)
                if not (low <= price <= high):
                    problems.append(f"price {price} is outside the band [{low}, {high}]")
        return problems

    def notional(self, *, qty: Decimal, price: Decimal) -> Decimal:
        return qty * price


#: Sensible defaults for the coins this repository trades, so a crypto instrument can be
#: built from ``symbols.yaml`` without a second source of truth.
def crypto_instrument(
    symbol: str, *, tick_size: Decimal, lot_step: Decimal, min_qty: Decimal
) -> Instrument:
    return Instrument(
        symbol=symbol,
        tick_size=tick_size,
        lot_size=lot_step,
        min_qty=min_qty,
        price_band_pct=None,  # no circuit limits on crypto
        product=Product.NORMAL,
    )
