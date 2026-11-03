"""Pre-trade risk engine: 17 checks, in order, every single order.

The order of the checks matters (cheap and safety-critical first) and this module
mirrors section 7.1 of the plan one-to-one. Every rejection is logged with its
reason and stored, so a rejected trade can always be explained afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..config import Config, RiskLimits
from ..core.logging import get_logger
from ..core.models import (
    KillLevel,
    MarketRules,
    OrderBook,
    RiskCheck,
    RiskVerdict,
    Side,
    Signal,
)
from ..core.money import ZERO, D, to_str
from ..portfolio.sizing import size_entry
from .killswitch import KillSwitch

log = get_logger(__name__)


@dataclass(slots=True)
class RiskContext:
    """A point-in-time snapshot of everything the risk engine needs."""

    now: datetime
    equity: Decimal
    starting_capital: Decimal
    day_start_equity: Decimal
    week_start_equity: Decimal
    peak_equity: Decimal
    symbol_exposure: Decimal = ZERO
    total_exposure: Decimal = ZERO
    bucket_exposure: Decimal = ZERO
    open_positions: int = 0
    orders_last_minute: int = 0
    orders_today: int = 0
    data_age_seconds: float = 0.0
    mid: Decimal | None = None
    spread_pct: Decimal = ZERO
    book: OrderBook | None = None
    cooldown_until: datetime | None = None
    symbol_enabled: bool = True
    strategy_paused: bool = False
    mode_allows_entries: bool = True
    ramp_multiplier: Decimal = Decimal("1")
    size_multiplier: Decimal = Decimal("1")
    available_cash: Decimal = ZERO
    #: fees for both legs plus the expected slippage, in basis points. Zero means
    #: "unknown", and the cost check is skipped rather than guessing.
    round_trip_cost_bps: Decimal = ZERO
    notes: list[str] = field(default_factory=list)

    # --- derived ----------------------------------------------------------
    @property
    def daily_pnl_pct(self) -> Decimal:
        if self.day_start_equity <= 0:
            return ZERO
        return (self.equity - self.day_start_equity) / self.day_start_equity

    @property
    def weekly_pnl_pct(self) -> Decimal:
        if self.week_start_equity <= 0:
            return ZERO
        return (self.equity - self.week_start_equity) / self.week_start_equity

    @property
    def drawdown_pct(self) -> Decimal:
        if self.peak_equity <= 0:
            return ZERO
        return (self.equity - self.peak_equity) / self.peak_equity


@dataclass(slots=True)
class EntryDecision:
    verdict: RiskVerdict
    qty: Decimal = ZERO
    entry_price: Decimal | None = None
    notional: Decimal = ZERO
    binding: str = ""

    @property
    def approved(self) -> bool:
        return self.verdict.approved and self.qty > 0


class RiskEngine:
    def __init__(self, config: Config, killswitch: KillSwitch) -> None:
        self.cfg = config
        self.limits: RiskLimits = config.risk
        self.killswitch = killswitch
        self.portfolio_cfg = config.settings.portfolio

    # --- main entry point -------------------------------------------------
    def approve_entry(self, signal: Signal, ctx: RiskContext, rules: MarketRules) -> EntryDecision:
        checks: list[RiskCheck] = []

        def add(name: str, passed: bool, detail: str = "") -> bool:
            checks.append(RiskCheck(name=name, passed=passed, detail=detail))
            return passed

        price = signal.limit_price or ctx.mid or ZERO
        stop_distance = price - signal.stop
        size = size_entry(
            price=price,
            stop=signal.stop,
            equity=ctx.equity,
            starting_capital=ctx.starting_capital,
            limits=self.limits,
            rules=rules,
            book=ctx.book,
            symbol_exposure=ctx.symbol_exposure,
            total_exposure=ctx.total_exposure,
            bucket_exposure=ctx.bucket_exposure,
            ramp_multiplier=ctx.ramp_multiplier,
            size_multiplier=ctx.size_multiplier,
            available_cash=ctx.available_cash or None,
        )

        # 1. kill switch
        kill_ok = add(
            "kill_switch",
            self.killswitch.level is KillLevel.L0,
            f"kill level {self.killswitch.level.value}",
        )
        # 2. mode permissions and ramp multiplier (paper always, live needs guards)
        mode_ok = add(
            "mode_permissions",
            ctx.mode_allows_entries,
            "mode allows new entries" if ctx.mode_allows_entries else "mode blocks new entries",
        )
        # 3. data freshness
        stale_limit = self.cfg.settings.data_quality.stale_symbol_seconds
        data_ok = add(
            "data_fresh",
            ctx.data_age_seconds <= stale_limit,
            f"feed age {ctx.data_age_seconds:.1f}s (limit {stale_limit}s)",
        )
        # 4. symbol enabled and strategy active
        symbol_ok = add(
            "symbol_enabled",
            ctx.symbol_enabled and not ctx.strategy_paused,
            "symbol enabled" if ctx.symbol_enabled else "symbol disabled",
        )
        # 5. spread
        spread_ok = add(
            "spread",
            ctx.spread_pct <= self.limits.max_spread_pct,
            f"spread {to_str(ctx.spread_pct * 100, 4)}% (max {to_str(self.limits.max_spread_pct * 100, 3)}%)",
        )
        # 6. fat finger: the proposed entry must be near the live mid
        if ctx.mid and price > 0:
            deviation = abs(price - ctx.mid) / ctx.mid
        else:
            deviation = ZERO
        fat_finger_ok = add(
            "fat_finger",
            ctx.mid is None or deviation <= self.limits.fat_finger_pct,
            f"price deviation {to_str(deviation * 100, 4)}%",
        )
        # 7. notional window
        notional_ok = add(
            "notional_window",
            size.ok
            and self.limits.min_notional <= size.notional <= self.limits.max_notional,
            f"notional {to_str(size.notional, 4)} in [{self.limits.min_notional}, {self.limits.max_notional}]",
        )
        # 8. risk per trade
        risk_pct = (size.qty * stop_distance) / ctx.equity if ctx.equity > 0 else Decimal("1")
        risk_ok = add(
            "risk_per_trade",
            size.ok and risk_pct <= self.limits.risk_per_trade,
            f"risk {to_str(risk_pct * 100, 4)}% of equity (max "
            f"{to_str(self.limits.risk_per_trade * 100, 3)}%)",
        )
        # 9. per symbol exposure
        sym_after = ctx.symbol_exposure + size.notional
        symbol_exp_ok = add(
            "per_symbol_exposure",
            ctx.equity > 0 and sym_after <= self.limits.per_symbol_exposure * ctx.equity,
            f"{to_str(sym_after, 4)} vs cap {to_str(self.limits.per_symbol_exposure * ctx.equity, 4)}",
        )
        # 10. total exposure
        total_after = ctx.total_exposure + size.notional
        total_ok = add(
            "total_exposure",
            ctx.equity > 0 and total_after <= self.limits.total_exposure * ctx.equity,
            f"{to_str(total_after, 4)} vs cap {to_str(self.limits.total_exposure * ctx.equity, 4)}",
        )
        # 11. correlated bucket
        bucket_after = ctx.bucket_exposure + size.notional
        bucket_ok = add(
            "bucket_exposure",
            ctx.equity > 0 and bucket_after <= self.limits.correlated_bucket_exposure * ctx.equity,
            f"{to_str(bucket_after, 4)} vs cap "
            f"{to_str(self.limits.correlated_bucket_exposure * ctx.equity, 4)}",
        )
        # 12. open positions
        positions_ok = add(
            "max_open_positions",
            ctx.open_positions < self.limits.max_open_positions,
            f"{ctx.open_positions} open (max {self.limits.max_open_positions})",
        )
        # 13. order size vs top-10 book depth
        depth_ok = True
        depth_detail = "no book"
        if ctx.book is not None and size.notional > 0:
            depth = ctx.book.notional_depth(Side.BUY, 10)
            depth_ok = size.notional <= self.limits.max_order_book_pct * depth
            depth_detail = (
                f"size {to_str(size.notional, 4)} vs {to_str(self.limits.max_order_book_pct * 100, 1)}% "
                f"of {to_str(depth, 4)} depth"
            )
        checks.append(RiskCheck("book_depth", depth_ok, depth_detail))
        # 14. daily / weekly loss limits
        losses_ok = add(
            "loss_limits",
            ctx.daily_pnl_pct > self.limits.daily_loss_stop_new_entries
            and ctx.weekly_pnl_pct > self.limits.weekly_loss_pause,
            f"day {to_str(ctx.daily_pnl_pct * 100, 3)}%, week {to_str(ctx.weekly_pnl_pct * 100, 3)}%",
        )
        # 15. cooldown after a stop-out
        cooldown_ok = add(
            "cooldown",
            ctx.cooldown_until is None or ctx.cooldown_until <= ctx.now,
            f"cooldown until {ctx.cooldown_until.isoformat()}" if ctx.cooldown_until else "no cooldown",
        )
        # 16. order rate caps
        rate_ok = add(
            "order_rate",
            ctx.orders_last_minute < self.limits.orders_per_minute
            and ctx.orders_today < self.limits.orders_per_day,
            f"{ctx.orders_last_minute}/min, {ctx.orders_today}/day",
        )
        # 17. cost budget: the target has to be reachable after fees and slippage
        cost_ok, cost_detail = self._cost_budget(signal, price, ctx)
        add("cost_budget", cost_ok, cost_detail)

        approved = all(
            (kill_ok, mode_ok, data_ok, symbol_ok, spread_ok, fat_finger_ok, notional_ok,
             risk_ok, symbol_exp_ok, total_ok, bucket_ok, positions_ok, depth_ok, losses_ok,
             cooldown_ok, rate_ok, cost_ok)
        )
        first_fail = next((c for c in checks if not c.passed), None)
        if approved:
            reason = "approved"
        elif first_fail is not None:
            reason = f"{first_fail.name}: {first_fail.detail}"
        else:  # pragma: no cover - defensive
            reason = "vetoed"
        verdict = RiskVerdict(
            approved=approved,
            reason=reason,
            checks=checks,
            adjusted_qty=size.qty if approved else ZERO,
        )
        if not approved:
            log.info(
                '{"event":"risk_veto","signal":"%s","symbol":"%s","reason":"%s"}',
                signal.id,
                signal.symbol,
                reason,
            )
        return EntryDecision(
            verdict=verdict,
            qty=size.qty if approved else ZERO,
            entry_price=price,
            notional=size.notional if approved else ZERO,
            binding=size.binding,
        )

    # --- exits are never blocked by entry gates ---------------------------
    def _cost_budget(
        self, signal: Signal, price: Decimal, ctx: RiskContext
    ) -> tuple[bool, str]:
        """Would the target survive the round-trip cost, or is this trade dead on arrival?

        Costs are not a detail at 15-minute horizons: two taker fees plus slippage can be
        most of the distance to the target, and a trade that needs the whole move to break
        even is a donation with extra steps. The requirement is a multiple, not a margin,
        because a target barely above cost has no room for the stop to be hit first.

        Disabled (and skipped, with no opinion claimed) when the ratio is 0, when the
        signal has no target, or when the cost estimate is unknown.
        """
        ratio = D(str(self.limits.min_target_cost_ratio))
        if ratio <= 0:
            return True, "no cost budget configured"
        if signal.target is None or price <= 0:
            return True, "no target to measure"
        cost_bps = ctx.round_trip_cost_bps
        if cost_bps <= 0:
            return True, "cost estimate unavailable"
        target_bps = abs(signal.target - price) / price * Decimal("10000")
        needed = cost_bps * ratio
        ok = target_bps >= needed
        return ok, (
            f"target {to_str(target_bps, 1)} bps vs {to_str(needed, 1)} bps "
            f"({to_str(ratio, 1)}x round-trip cost {to_str(cost_bps, 1)} bps)"
        )

    def approve_exit(self, symbol: str) -> RiskVerdict:
        """Closing risk is always allowed, at any kill level, even mid-flatten."""
        return RiskVerdict(
            approved=True,
            reason="exit",
            checks=[RiskCheck("exit", True, "exits always permitted")],
        )

    # --- account-level monitors ------------------------------------------
    def account_alerts(self, ctx: RiskContext) -> list[tuple[str, KillLevel]]:
        """Account-level triggers evaluated every loop (section 7.2 / 7.4)."""
        out: list[tuple[str, KillLevel]] = []
        if ctx.drawdown_pct <= self.limits.max_drawdown_lockdown:
            out.append((f"drawdown {to_str(ctx.drawdown_pct * 100, 2)}%", KillLevel.L3))
        if ctx.daily_pnl_pct <= self.limits.daily_loss_flatten:
            out.append((f"daily loss {to_str(ctx.daily_pnl_pct * 100, 2)}%", KillLevel.L2))
        elif ctx.daily_pnl_pct <= self.limits.daily_loss_stop_new_entries:
            out.append((f"daily loss {to_str(ctx.daily_pnl_pct * 100, 2)}%", KillLevel.L1))
        if ctx.weekly_pnl_pct <= self.limits.weekly_loss_pause:
            out.append((f"weekly loss {to_str(ctx.weekly_pnl_pct * 100, 2)}%", KillLevel.L1))
        return out
