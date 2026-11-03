"""Position sizing.

Risk first: size = risk budget / stop distance. Then every exposure cap, the
notional cap and the order-book cap are applied, and the *smallest* wins. Section
17.2 of the plan warns that the caps will often bind - that is intended, and the
binding constraint is recorded so the decision is explainable later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..config import RiskLimits
from ..core.models import MarketRules, OrderBook, Side
from ..core.money import ZERO, D, round_qty, to_str


@dataclass(slots=True)
class SizeResult:
    qty: Decimal = ZERO
    risk_amount: Decimal = ZERO
    stop_distance: Decimal = ZERO
    notional: Decimal = ZERO
    binding: str = "none"
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.qty > 0

    def to_dict(self) -> dict[str, str]:
        return {
            "qty": to_str(self.qty, 8),
            "risk_amount": to_str(self.risk_amount, 6),
            "stop_distance": to_str(self.stop_distance, 8),
            "notional": to_str(self.notional, 6),
            "binding": self.binding,
        }


def sizing_equity(equity: Decimal, starting_capital: Decimal, limits: RiskLimits) -> Decimal:
    """Compounding is allowed but capped, so a hot streak cannot inflate risk."""
    return min(equity, starting_capital * limits.compounding_cap_mult)


def size_entry(
    *,
    price: Decimal,
    stop: Decimal,
    equity: Decimal,
    starting_capital: Decimal,
    limits: RiskLimits,
    rules: MarketRules,
    book: OrderBook | None = None,
    symbol_exposure: Decimal = ZERO,
    total_exposure: Decimal = ZERO,
    bucket_exposure: Decimal = ZERO,
    ramp_multiplier: Decimal = Decimal("1"),
    size_multiplier: Decimal = Decimal("1"),
    available_cash: Decimal | None = None,
) -> SizeResult:
    """Return the largest size that satisfies every cap. Never rounds up."""
    result = SizeResult()
    if price <= 0:
        result.binding = "invalid_price"
        return result
    stop_distance = price - stop
    result.stop_distance = stop_distance
    if stop_distance <= 0:
        result.binding = "invalid_stop"
        return result

    eq_for_sizing = sizing_equity(equity, starting_capital, limits)
    risk_budget = eq_for_sizing * limits.risk_per_trade * ramp_multiplier * size_multiplier
    result.risk_amount = risk_budget
    qty = risk_budget / stop_distance
    result.binding = "risk_per_trade"
    result.notes.append(f"risk budget {to_str(risk_budget, 6)} / stop {to_str(stop_distance, 8)}")

    def cap(cap_qty: Decimal, name: str) -> None:
        nonlocal qty
        if cap_qty < 0:
            cap_qty = ZERO
        if cap_qty < qty:
            qty = cap_qty
            result.binding = name

    # exposure caps are measured against current equity, not sizing equity
    cap((limits.per_symbol_exposure * equity - symbol_exposure) / price, "per_symbol_exposure")
    cap((limits.total_exposure * equity - total_exposure) / price, "total_exposure")
    cap((limits.correlated_bucket_exposure * equity - bucket_exposure) / price, "bucket_exposure")
    cap(limits.max_notional / price, "max_notional")
    if available_cash is not None:
        # never try to spend cash we do not have (fees included in the haircut)
        cap(max(available_cash, ZERO) * D("0.995") / price, "available_cash")
    if book is not None and book.asks and book.bids:
        depth = book.notional_depth(Side.BUY, 10)
        if depth > 0:
            cap(limits.max_order_book_pct * depth / price, "book_depth")

    qty = round_qty(qty, rules.lot_step)
    floor_notional = max(rules.min_notional, limits.min_notional)
    result.qty = qty
    result.notional = qty * price
    if qty <= 0 or qty < rules.min_qty:
        result.qty = ZERO
        result.notional = ZERO
        result.notes.append(f"size below lot minimum ({rules.min_qty})")
        result.binding = result.binding if result.binding != "risk_per_trade" else "lot_min"
        return result
    if result.notional < floor_notional:
        result.qty = ZERO
        result.notional = ZERO
        result.notes.append(f"notional {to_str(qty * price, 6)} below floor {floor_notional}")
        result.binding = "min_notional"
    return result


def exposure_after(size: SizeResult, current: Decimal) -> Decimal:
    return current + size.notional
