"""Position and PnL ledger. Decimal only, long-only spot.

The ledger is the strategy-side view of the account (positions, realized PnL,
fees, equity). The adapter holds the venue-side view. Reconciliation compares the
two; they must never silently diverge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from ..core.clock import utcnow
from ..core.models import Fill, Position, Side
from ..core.money import ZERO, D, to_str


@dataclass(slots=True)
class EntryPlan:
    """Everything the ledger needs to open a position when an entry fill lands."""

    symbol: str
    strategy: str
    stop: Decimal
    target: Decimal
    signal_id: str = ""
    atr: Decimal = ZERO
    bars_held: int = 0


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    symbol: str
    strategy: str
    qty: Decimal
    entry: Decimal
    exit: Decimal
    pnl: Decimal
    fees: Decimal
    opened_at: datetime
    closed_at: datetime
    reason: str
    r_multiple: Decimal
    signal_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "qty": to_str(self.qty),
            "entry": to_str(self.entry),
            "exit": to_str(self.exit),
            "pnl": to_str(self.pnl, 8),
            "fees": to_str(self.fees, 8),
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat(),
            "reason": self.reason,
            "r_multiple": to_str(self.r_multiple, 4),
            "signal_id": self.signal_id,
        }


@dataclass
class Ledger:
    starting_cash: Decimal
    quote_currency: str = "USDT"
    cash: Decimal = field(default=None)  # type: ignore[assignment]
    realized_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO
    positions: dict[tuple[str, str], Position] = field(default_factory=dict)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    tax_reserve: Decimal = ZERO
    tds_paid: Decimal = ZERO
    gross_sales: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.cash is None:
            self.cash = self.starting_cash

    # --- views ------------------------------------------------------------
    @property
    def open_positions(self) -> list[Position]:
        return list(self.positions.values())

    def get(self, symbol: str, strategy: str | None = None) -> Position | None:
        if strategy is not None:
            return self.positions.get((symbol, strategy))
        for (sym, _strat), pos in self.positions.items():
            if sym == symbol:
                return pos
        return None

    def positions_for(self, symbol: str) -> list[Position]:
        return [p for (sym, _), p in self.positions.items() if sym == symbol]

    def exposure(self, marks: dict[str, Decimal]) -> Decimal:
        return sum((p.qty * marks.get(p.symbol, p.avg_entry) for p in self.positions.values()), ZERO)

    def positions_value(self, marks: dict[str, Decimal]) -> Decimal:
        return self.exposure(marks)

    def unrealized(self, marks: dict[str, Decimal]) -> Decimal:
        return sum(
            (p.unrealized(marks.get(p.symbol, p.avg_entry)) for p in self.positions.values()), ZERO
        )

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        return self.cash + self.positions_value(marks)

    def gross_exposure_pct(self, marks: dict[str, Decimal]) -> Decimal:
        eq = self.equity(marks)
        if eq <= 0:
            return ZERO
        return self.exposure(marks) / eq

    def symbol_exposure(self, symbol: str, marks: dict[str, Decimal]) -> Decimal:
        return sum((p.qty * marks.get(symbol, p.avg_entry) for p in self.positions_for(symbol)), ZERO)

    def bucket_exposure(self, bucket_symbols: list[str], marks: dict[str, Decimal]) -> Decimal:
        return sum((self.symbol_exposure(s, marks) for s in bucket_symbols), ZERO)

    # --- fills ------------------------------------------------------------
    def on_fill(self, fill: Fill, plan: EntryPlan | None = None, mark_price: Decimal | None = None) -> ClosedTrade | None:
        """Apply a fill. Returns a ClosedTrade when the fill closes a position."""
        if fill.side is Side.BUY:
            self._apply_buy(fill, plan)
            return None
        return self._apply_sell(fill, mark_price)

    def _apply_buy(self, fill: Fill, plan: EntryPlan | None) -> None:
        key = (fill.symbol, fill.strategy)
        pos = self.positions.get(key)
        if pos is None and plan is None:
            # validate before touching cash: a half-applied fill corrupts the ledger
            raise ValueError(f"entry fill for {fill.symbol}/{fill.strategy} without an EntryPlan")
        notional = fill.price * fill.qty
        self.cash -= notional + fill.fee
        self.fees_paid += fill.fee
        if pos is None:
            plan = cast(EntryPlan, plan)  # the guard above proved it exists
            pos = Position(
                symbol=fill.symbol,
                strategy=fill.strategy,
                qty=fill.qty,
                avg_entry=fill.price,
                stop=plan.stop,
                target=plan.target,
                initial_stop=plan.stop,
                opened_at=fill.ts,
                atr_at_entry=plan.atr,
                risk_per_unit=(fill.price - plan.stop),
                initial_qty=fill.qty,
                highest_price=fill.price,
                fees=fill.fee,
            )
            self.positions[key] = pos
        else:
            total_qty = pos.qty + fill.qty
            pos.avg_entry = (pos.avg_entry * pos.qty + fill.price * fill.qty) / total_qty
            pos.qty = total_qty
            pos.initial_qty += fill.qty
            pos.fees += fill.fee
            pos.highest_price = max(pos.highest_price, fill.price)

    def _apply_sell(self, fill: Fill, mark_price: Decimal | None) -> ClosedTrade | None:
        key = (fill.symbol, fill.strategy)
        pos = self.positions.get(key)
        if pos is None:
            raise ValueError(f"exit fill for {fill.symbol}/{fill.strategy} with no open position")
        if fill.qty > pos.qty:
            raise ValueError(
                f"exit qty {fill.qty} exceeds position {pos.qty} for {fill.symbol}/{fill.strategy}"
            )
        notional = fill.price * fill.qty
        self.cash += notional - fill.fee
        self.fees_paid += fill.fee
        self.gross_sales += notional
        self.tds_paid += notional * D("0.01")  # informational: 1% TDS on sales (India)
        pnl = (fill.price - pos.avg_entry) * fill.qty - fill.fee
        pos.realized_pnl += pnl
        pos.fees += fill.fee
        self.realized_pnl += pnl
        pos.qty -= fill.qty
        if pos.qty <= 0:
            closed = ClosedTrade(
                symbol=pos.symbol,
                strategy=pos.strategy,
                qty=pos.initial_qty,
                entry=pos.avg_entry,
                exit=fill.price,
                pnl=pos.realized_pnl,
                fees=pos.fees,
                opened_at=pos.opened_at,
                closed_at=fill.ts,
                reason=fill.reason or "exit",
                r_multiple=pos.r_multiple(fill.price),
            )
            self.closed_trades.append(closed)
            del self.positions[key]
            return closed
        return None

    # --- tax reserve (section 7.3) ---------------------------------------
    def accrue_tax_reserve(self, pct: Decimal) -> Decimal:
        """Virtual 30% reserve on realised gains. The bot never moves money."""
        gains = sum((t.pnl for t in self.closed_trades if t.pnl > 0), ZERO)
        target = max(gains * pct, ZERO)
        delta = max(target - self.tax_reserve, ZERO)
        self.tax_reserve += delta
        return delta

    # --- invariants -------------------------------------------------------
    def check_invariant(self, marks: dict[str, Decimal]) -> tuple[bool, str]:
        """cash + position value must equal equity, and cash must not go negative."""
        eq = self.equity(marks)
        recomputed = self.cash + self.positions_value(marks)
        if eq != recomputed:
            return False, f"equity mismatch: {eq} != {recomputed}"
        if self.cash < 0:
            return False, f"negative cash: {self.cash}"
        for pos in self.positions.values():
            if pos.qty < 0:
                return False, f"negative qty on {pos.symbol}/{pos.strategy}"
        return True, "ok"

    def reconcile_with_adapter(self, adapter_cash: Decimal, tolerance: Decimal = D("0.000001")) -> tuple[bool, str]:
        diff = self.cash - adapter_cash
        if abs(diff) > tolerance:
            return False, f"cash mismatch: ledger {self.cash} vs exchange {adapter_cash} (diff {diff})"
        return True, "ok"

    def snapshot(self, marks: dict[str, Decimal]) -> dict[str, Any]:
        return {
            "cash": to_str(self.cash, 8),
            "equity": to_str(self.equity(marks), 8),
            "realized_pnl": to_str(self.realized_pnl, 8),
            "unrealized_pnl": to_str(self.unrealized(marks), 8),
            "fees_paid": to_str(self.fees_paid, 8),
            "open_positions": len(self.positions),
            "closed_trades": len(self.closed_trades),
            "tax_reserve": to_str(self.tax_reserve, 8),
            "ts": utcnow().isoformat(),
        }
