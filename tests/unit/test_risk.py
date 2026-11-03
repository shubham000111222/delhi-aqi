"""Risk engine checks, kill-switch behaviour and live-mode guards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_book

from tradebot.config import Mode
from tradebot.core.clock import SimClock
from tradebot.core.models import (
    KillLevel,
    MarketRules,
    OrderType,
    Side,
    Signal,
)
from tradebot.core.money import D
from tradebot.core.store import Store
from tradebot.risk.engine import RiskContext, RiskEngine
from tradebot.risk.killswitch import KillSwitch
from tradebot.risk.live_guards import LiveGuardChecker, LiveGuardError, RampController

NOW = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


def _rules() -> MarketRules:
    return MarketRules("BTC/USDT", D("0.1"), D("0.00001"), D("0.00001"), D("5"), D("0.001"), D("0.001"))


def _signal(**kw) -> Signal:
    defaults = dict(
        id="sig1", strategy="S1_ema_trend", symbol="BTC/USDT", timeframe="15m",
        side=Side.BUY, entry_type=OrderType.LIMIT, ts=NOW,
        stop=D("58000"), target=D("64000"), limit_price=D("60000"),
    )
    defaults.update(kw)
    return Signal(**defaults)


def _ctx(**kw) -> RiskContext:
    defaults = dict(
        now=NOW,
        equity=D("1000"),
        starting_capital=D("1000"),
        day_start_equity=D("1000"),
        week_start_equity=D("1000"),
        peak_equity=D("1000"),
        mid=D("60000"),
        spread_pct=D("0.0004"),
        book=make_book(),
        data_age_seconds=1.0,
        available_cash=D("1000"),
    )
    defaults.update(kw)
    return RiskContext(**defaults)


@pytest.fixture
def engine(cfg) -> RiskEngine:
    store = Store()
    return RiskEngine(cfg, KillSwitch(store, clock=SimClock(NOW)))


class TestCostBudget:
    """A trade whose target cannot clear the round-trip cost is dead on arrival."""

    def test_a_near_target_against_high_costs_is_refused(self, engine):
        # 60,000 -> 60,300 is 50 bps; 4 x a 30 bps round trip needs 120 bps
        decision = engine.approve_entry(
            _signal(target=D("60300")), _ctx(round_trip_cost_bps=D("30")), _rules()
        )
        assert not decision.approved
        failed = {c.name for c in decision.verdict.failed}
        assert "cost_budget" in failed
        detail = next(c.detail for c in decision.verdict.checks if c.name == "cost_budget")
        assert "50.0 bps" in detail and "120.0 bps" in detail

    def test_a_target_that_clears_the_cost_is_allowed(self, engine):
        decision = engine.approve_entry(
            _signal(target=D("62000")), _ctx(round_trip_cost_bps=D("30")), _rules()
        )
        assert decision.approved, decision.verdict.reason

    def test_the_check_is_off_at_ratio_zero(self, engine):
        engine.limits.min_target_cost_ratio = D("0")
        decision = engine.approve_entry(
            _signal(target=D("60100")), _ctx(round_trip_cost_bps=D("100")), _rules()
        )
        assert decision.approved, decision.verdict.reason

    def test_an_unknown_cost_is_not_treated_as_free_but_is_also_not_a_block(self, engine):
        decision = engine.approve_entry(_signal(target=D("60010")), _ctx(), _rules())
        assert decision.approved
        detail = next(c.detail for c in decision.verdict.checks if c.name == "cost_budget")
        assert "unavailable" in detail

    def test_a_signal_without_a_target_is_not_blocked(self, engine):
        decision = engine.approve_entry(
            _signal(target=None), _ctx(round_trip_cost_bps=D("500")), _rules()
        )
        assert decision.approved, decision.verdict.reason

    def test_a_short_target_is_measured_by_distance_not_direction(self, engine):
        # selling: 60,000 -> 59,700 is still 50 bps away, and still too near
        signal = _signal(side=Side.SELL, target=D("59700"))
        decision = engine.approve_entry(signal, _ctx(round_trip_cost_bps=D("30")), _rules())
        assert not decision.approved
        assert "cost_budget" in decision.verdict.reason


class TestRiskChecks:
    def test_happy_path_approves(self, engine):
        decision = engine.approve_entry(_signal(), _ctx(), _rules())
        assert decision.approved, decision.verdict.reason
        assert decision.qty > 0
        assert all(c.passed for c in decision.verdict.checks)
        assert len(decision.verdict.checks) == 17

    def test_kill_switch_blocks(self, engine):
        engine.killswitch.trigger(KillLevel.L1, trigger="unit", reason="paused")
        decision = engine.approve_entry(_signal(), _ctx(), _rules())
        assert not decision.approved
        assert "kill_switch" in decision.verdict.reason

    def test_stale_data_blocks(self, engine):
        decision = engine.approve_entry(_signal(), _ctx(data_age_seconds=60.0), _rules())
        assert not decision.approved
        assert "data_fresh" in decision.verdict.reason

    def test_disabled_symbol_blocks(self, engine):
        decision = engine.approve_entry(_signal(), _ctx(symbol_enabled=False), _rules())
        assert not decision.approved
        assert "symbol_enabled" in decision.verdict.reason

    def test_wide_spread_blocks(self, engine):
        decision = engine.approve_entry(_signal(), _ctx(spread_pct=D("0.01")), _rules())
        assert not decision.approved
        assert "spread" in decision.verdict.reason

    def test_fat_finger_blocks(self, engine):
        signal = _signal(limit_price=D("66000"))  # 10% away from the mid
        decision = engine.approve_entry(signal, _ctx(), _rules())
        assert not decision.approved
        assert "fat_finger" in decision.verdict.reason

    def test_exposure_cap_blocks(self, engine):
        ctx = _ctx(symbol_exposure=D("199"), total_exposure=D("199"), bucket_exposure=D("199"))
        decision = engine.approve_entry(_signal(), ctx, _rules())
        assert not decision.approved
        failed = {c.name for c in decision.verdict.failed}
        assert failed & {"per_symbol_exposure", "total_exposure", "bucket_exposure", "notional_window"}

    def test_max_positions_blocks(self, engine):
        decision = engine.approve_entry(_signal(), _ctx(open_positions=5), _rules())
        assert not decision.approved
        assert "max_open_positions" in decision.verdict.reason

    def test_daily_loss_limit_blocks(self, engine):
        decision = engine.approve_entry(_signal(), _ctx(equity=D("970"), day_start_equity=D("1000")), _rules())
        assert not decision.approved
        assert "loss_limits" in decision.verdict.reason

    def test_cooldown_blocks(self, engine):
        decision = engine.approve_entry(
            _signal(), _ctx(cooldown_until=NOW + timedelta(minutes=5)), _rules()
        )
        assert not decision.approved
        assert "cooldown" in decision.verdict.reason

    def test_order_rate_blocks(self, engine):
        decision = engine.approve_entry(_signal(), _ctx(orders_last_minute=99), _rules())
        assert not decision.approved
        assert "order_rate" in decision.verdict.reason

    def test_thin_book_blocks(self, engine):
        ctx = _ctx(book=make_book(qty_per_level="0.0001"))
        decision = engine.approve_entry(_signal(), ctx, _rules())
        assert not decision.approved
        assert "book_depth" in decision.verdict.reason or "notional_window" in decision.verdict.reason

    def test_exits_are_always_allowed(self, engine):
        engine.killswitch.trigger(KillLevel.L2, trigger="unit", reason="flattening")
        assert engine.approve_exit("BTC/USDT").approved

    def test_account_alerts_escalate(self, engine):
        alerts = engine.account_alerts(_ctx(equity=D("965"), day_start_equity=D("1000")))
        levels = [level for _, level in alerts]
        assert KillLevel.L2 in levels
        drawdown = engine.account_alerts(_ctx(equity=D("890"), peak_equity=D("1000")))
        assert any(level is KillLevel.L3 for _, level in drawdown)


class TestKillSwitch:
    def test_escalates_but_never_auto_de_escalates(self):
        ks = KillSwitch(Store(), clock=SimClock(NOW))
        assert ks.level is KillLevel.L0
        assert ks.trigger(KillLevel.L1, trigger="t", reason="r")
        assert not ks.trigger(KillLevel.L0, trigger="t", reason="r")
        assert ks.level is KillLevel.L1
        assert ks.trigger(KillLevel.L2, trigger="t", reason="r")
        assert ks.level is KillLevel.L2
        assert not ks.can_open_entries()
        assert ks.must_flatten()

    def test_reset_returns_to_l0(self):
        ks = KillSwitch(Store(), clock=SimClock(NOW))
        ks.trigger(KillLevel.L1, trigger="t", reason="r")
        ks.reset(reason="reviewed", actor="operator")
        assert ks.level is KillLevel.L0

    def test_l3_needs_reason_and_cooldown(self):
        clock = SimClock(NOW)
        ks = KillSwitch(Store(), clock=clock)
        ks.trigger(KillLevel.L3, trigger="drawdown", reason="dd")
        with pytest.raises(PermissionError):
            ks.reset(reason="too early", actor="op")
        clock.advance(timedelta(hours=2))
        with pytest.raises(PermissionError):
            ks.reset(reason="", actor="op")
        ks.reset(reason="root cause fixed", actor="op")
        assert ks.level is KillLevel.L0

    def test_kill_file_triggers_flatten(self, tmp_path):
        kill_file = tmp_path / "KILL"
        kill_file.write_text("manual stop: testing")
        ks = KillSwitch(Store(), clock=SimClock(NOW), kill_file=kill_file)
        assert ks.can_open_entries() is False
        assert ks.level is KillLevel.L2
        assert ks.state["trigger"] == "kill_file"

    def test_state_is_persisted(self, tmp_path):
        path = tmp_path / "k.db"
        store = Store(path)
        ks = KillSwitch(store, clock=SimClock(NOW))
        ks.trigger(KillLevel.L2, trigger="reconciliation", reason="mismatch")
        store.close()
        reopened = Store(path)
        assert KillSwitch(reopened, clock=SimClock(NOW)).level is KillLevel.L2


class TestLiveGuards:
    async def test_paper_mode_fails_closed(self, cfg):
        store = Store()
        checker = LiveGuardChecker(cfg, store)
        results = await checker.check()
        failed = {r.name for r in results if r.blocking and not r.passed}
        assert {"mode_live", "env_switch", "go_nogo_record", "api_key_permissions"} <= failed
        with pytest.raises(LiveGuardError):
            checker.require(results)

    async def test_confirmation_parsing(self, cfg):
        checker = LiveGuardChecker(cfg, Store())
        ok, _ = checker._check_confirmation("LIVE 250", D("250"))
        assert ok
        ok, _ = checker._check_confirmation("LIVE 999", D("250"))
        assert not ok
        ok, _ = checker._check_confirmation("yes", D("250"))
        assert not ok

    async def test_adapter_without_permission_report_fails(self, cfg):
        checker = LiveGuardChecker(cfg, Store())

        class NoPerms:
            pass

        result = await checker._check_api_key(NoPerms())
        assert not result.passed

    async def test_adapter_reporting_withdrawals_enabled_fails(self, cfg):
        checker = LiveGuardChecker(cfg, Store())

        class Withdrawals:
            async def verify_permissions(self, require_ip_whitelist: bool = True):
                return {"passed": False, "detail": "withdrawals enabled"}

        result = await checker._check_api_key(Withdrawals())
        assert not result.passed

    async def test_go_nogo_record_age(self, cfg):
        store = Store()
        checker = LiveGuardChecker(cfg, store)
        store.kv_set("go_nogo", {"ts": "2024-01-01T00:00:00Z", "passed": True})
        fresh = checker._check_go_nogo(datetime(2024, 1, 5, tzinfo=UTC))
        stale = checker._check_go_nogo(datetime(2024, 3, 1, tzinfo=UTC))
        assert fresh.passed and not stale.passed

    def test_ramp_stages(self, cfg):
        store = Store()
        ramp = RampController(cfg, store)
        state = ramp.load()
        assert state.stage == 0 and state.multiplier == D("0.10")
        # paper mode always gets full size
        assert ramp.multiplier() == D("1")

    def test_ramp_advances_after_min_days(self, cfg):
        store = Store()
        ramp = RampController(cfg, store)
        start = datetime(2024, 1, 1, tzinfo=UTC)
        store.kv_set("ramp", {"stage": 0, "stage_started": start.isoformat().replace("+00:00", "Z"), "clean_days": 0, "critical_incidents": 0})
        state = ramp.advance_if_eligible(start + timedelta(days=8))
        assert state.stage == 1 and state.multiplier == D("0.25")
        store.kv_set("ramp", {"stage": 1, "stage_started": start.isoformat().replace("+00:00", "Z"), "clean_days": 0, "critical_incidents": 1})
        state = ramp.advance_if_eligible(start + timedelta(days=30))
        assert state.stage == 1  # a critical incident blocks promotion


class TestConfigEnvelope:
    def test_risk_envelope_rejects_greedy_config(self, cfg):
        from tradebot.config import RiskLimits

        limits = RiskLimits(risk_per_trade=D("0.05"))
        with pytest.raises(ValueError):
            limits.validate_envelope()

    def test_unknown_key_rejected(self, cfg):
        from pydantic import ValidationError

        from tradebot.config import RiskLimits

        with pytest.raises(ValidationError):
            RiskLimits(not_a_limit=D("1"))

    def test_modes_are_distinct(self, cfg):
        cfg.settings.mode = Mode.LIVE
        assert cfg.settings.db_path.name == "tradebot_live.db"
        cfg.settings.mode = Mode.PAPER
        assert cfg.settings.db_path.name == "tradebot_paper.db"


class TestErrorRateBreaker:
    """One flaky order must never pause the bot; a failing venue still must."""

    async def _engine(self, cfg, clock):
        from tradebot.core.store import Store
        from tradebot.data.feed import SyntheticFeed
        from tradebot.engine import Engine

        store = Store(":memory:")
        return await Engine.build(cfg, clock=clock, store=store, feed=SyntheticFeed(cfg, clock))

    async def _health(self, engine, errors: int, submitted: int):
        now = engine.clock.now()
        for _ in range(submitted):
            engine.store.log_event("order_submitted", {"cid": "x"}, actor="test", ts=now)
        for _ in range(errors):
            engine.store.log_event("order_error", {"error": "x"}, level="WARN", actor="test", ts=now)
        return await engine.health_check()

    async def test_small_sample_does_not_pause(self, cfg):
        clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
        engine = await self._engine(cfg, clock)
        # Two failures out of five attempts is a 40% ratio, on almost no evidence.
        health = await self._health(engine, errors=2, submitted=3)
        assert health["order_attempts"] < cfg.risk.exchange_error_rate_min_samples
        assert health["kill_level"] == "L0", "a ratio on a handful of orders must not pause trading"

    async def test_real_outage_still_pauses(self, cfg):
        clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
        engine = await self._engine(cfg, clock)
        health = await self._health(engine, errors=25, submitted=40)
        assert health["kill_level"] == "L1"
        assert engine.killswitch.level is KillLevel.L1
        assert "exchange_error_rate" in engine.killswitch.state.get("trigger", "")


class TestAutoResume:
    """Recovery must match the plan: daily limit resets with the day, infrastructure
    faults lift after sustained health, and risk breaches need a human."""

    async def _engine(self, cfg, clock):
        from tradebot.data.feed import SyntheticFeed
        from tradebot.engine import Engine

        store = Store(":memory:")
        return await Engine.build(cfg, clock=clock, store=store, feed=SyntheticFeed(cfg, clock))

    def _breach_day(self, engine, day_start):
        engine.store.kv_set("day_start_equity", str(day_start))
        engine.store.kv_set("week_start_equity", str(day_start))

    async def test_daily_loss_pause_lifts_at_the_next_day(self, cfg):
        clock = SimClock(NOW)
        engine = await self._engine(cfg, clock)
        # a -2.5% day stops new entries, triggered by the engine's own limit check
        self._breach_day(engine, engine.ledger.cash * D("1.025"))
        engine._check_account_limits(engine.equity())
        assert engine.killswitch.level is KillLevel.L1
        assert engine.killswitch.state["trigger"] == "daily_loss"

        # still the same day: nothing lifts it
        await engine.health_check()
        engine._resume_daily_limit_at_day_boundary(clock.now())
        assert engine.killswitch.level is KillLevel.L1, "a risk pause must not lift early"

        # the day rolls over and the baseline is re-based, so the limit is satisfied
        clock.advance(2 * 86_400)
        self._breach_day(engine, engine.ledger.cash)
        engine._resume_daily_limit_at_day_boundary(clock.now())
        assert engine.killswitch.level is KillLevel.L0
        assert engine.killswitch.state["trigger"] == "auto_resume"
        assert any(
            e["kind"] == "kill_switch_auto_resume" for e in engine.store.events(limit=20)
        ), "an automatic resume must be in the audit log"

    async def test_daily_limit_holds_if_the_day_is_still_underwater(self, cfg):
        clock = SimClock(NOW)
        engine = await self._engine(cfg, clock)
        self._breach_day(engine, engine.ledger.cash * D("1.025"))
        engine._check_account_limits(engine.equity())
        for _ in range(3):
            clock.advance(86_400)
            engine._resume_daily_limit_at_day_boundary(clock.now())
        assert engine.killswitch.level is KillLevel.L1, "re-basing is checked, not assumed"

    async def test_weekly_loss_needs_a_human(self, cfg):
        clock = SimClock(NOW)
        engine = await self._engine(cfg, clock)
        self._breach_day(engine, engine.ledger.cash * D("1.025"))
        engine.killswitch.trigger(KillLevel.L1, trigger="weekly_loss", reason="weekly loss -5.20%")
        for _ in range(5):
            await engine.health_check()
            clock.advance(86_400)
            engine._resume_daily_limit_at_day_boundary(clock.now())
        assert engine.killswitch.level is KillLevel.L1

    async def test_manual_pause_never_auto_resumes(self, cfg):
        clock = SimClock(NOW)
        engine = await self._engine(cfg, clock)
        engine.killswitch.trigger(KillLevel.L1, trigger="manual", reason="operator pause")
        for _ in range(5):
            await engine.health_check()
            clock.advance(cfg.settings.execution.auto_resume_min_healthy_seconds)
        assert engine.killswitch.level is KillLevel.L1, "only a human may undo a human pause"

    async def test_flatten_levels_never_auto_resume(self, cfg):
        clock = SimClock(NOW)
        engine = await self._engine(cfg, clock)
        engine.killswitch.trigger(KillLevel.L2, trigger="daily_loss", reason="daily loss -3.50%")
        for _ in range(5):
            await engine.health_check()
            clock.advance(86_400)
            engine._resume_daily_limit_at_day_boundary(clock.now())
        assert engine.killswitch.level is KillLevel.L2

    async def test_infrastructure_pause_lifts_after_sustained_health(self, cfg):
        clock = SimClock(NOW)
        engine = await self._engine(cfg, clock)
        engine.killswitch.trigger(KillLevel.L1, trigger="exchange_error_rate", reason="storm")
        engine._reconcile_state = "ok"
        # one instant of health is not enough
        await engine.health_check()
        assert engine.killswitch.level is KillLevel.L1
        clock.advance(cfg.settings.execution.auto_resume_min_healthy_seconds + 1)
        await engine.health_check()
        assert engine.killswitch.level is KillLevel.L0
