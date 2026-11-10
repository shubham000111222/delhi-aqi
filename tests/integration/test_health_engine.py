"""The health model as the engine actually feeds it (§20.2, §20.4).

The unit tests pin the model; these pin the wiring, which is where a health check usually
rots: a component registered but never reported, a state change nobody audits, a restart
budget that never refuses anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tradebot.core.clock import SimClock
from tradebot.core.models import KillLevel
from tradebot.core.store import Store
from tradebot.engine import Engine

START = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
async def engine(fast_cfg):
    fast_cfg.ensure_dirs()
    built = await Engine.build(fast_cfg, clock=SimClock(START), store=Store(":memory:"))
    yield built
    built.close()


async def test_a_cold_engine_is_healthy_rather_than_degraded(engine):
    """Nothing has gone wrong before the first tick; saying DEGRADED here is a false alarm
    that teaches the reader to ignore the word."""
    health = await engine.health_check()

    components = health["components"]
    assert components["overall"] == "HEALTHY"
    assert components["components"]["feed"]["state"] == "HEALTHY"
    assert "not sampled" in components["components"]["feed"]["checks"]["stream"]["detail"]


async def test_every_component_reports_all_three_questions(engine):
    health = await engine.health_check()

    registered = set(engine.health.components)
    assert {"feed", "candles", "strategies", "orders", "store", "adapter"} <= registered
    for name, info in health["components"]["components"].items():
        assert info["state"] in {"HEALTHY", "DEGRADED", "FAILED"}
        for check in info["checks"].values():
            assert {"liveness", "readiness", "correctness"} == set(check) & {
                "liveness",
                "readiness",
                "correctness",
            }, name


async def test_a_stale_feed_degrades_the_feed_and_is_audited(engine):
    await engine.run(stop_at=START + timedelta(hours=6))
    assert engine.stats.bars > 0

    # no new bars for an hour: every symbol is stale, which is the real thing this check
    # exists for. The kill switch also fires here - that is the documented behaviour.
    engine.clock = SimClock(START + timedelta(hours=7))
    health = await engine.health_check()

    assert health["components"]["components"]["feed"]["state"] == "DEGRADED"
    assert health["components"]["components"]["candles"]["state"] == "DEGRADED"
    kinds = [row["kind"] for row in engine.store._query("select kind from events")]
    assert "health_change" in kinds, "a state change without an event is a number nobody reads"


async def test_the_third_fault_pauses_and_the_fourth_disables_the_strategy(engine):
    name = "S1_ema_trend"

    for _ in range(3):
        engine._handle_strategy_fault(name, reason="exception: boom")

    assert name not in engine._disabled_strategies
    assert engine._strategy_blocked(name) is True, "a paused strategy must not trade"

    engine._handle_strategy_fault(name, reason="exception: boom")

    assert name in engine._disabled_strategies
    assert engine.strategy_faults.used(name, now=engine.clock.now()) == 3
    kinds = [row["kind"] for row in engine.store._query("select kind from events")]
    assert "strategy_disabled" in kinds

    # the strategy stays blocked even after the one-hour pause would have expired
    engine.clock = SimClock(START + timedelta(hours=48))
    assert engine._strategy_blocked(name) is True
    health = await engine.health_check()
    assert health["components"]["components"]["strategies"]["state"] == "FAILED"


async def test_the_daily_report_carries_the_health_line(engine):
    await engine.run(stop_at=START + timedelta(hours=2))
    report = await engine.emit_daily_report()

    markdown = Path(report["paths"]["markdown"]).read_text(encoding="utf-8")
    assert "Component health:" in markdown
    line = markdown.split("Component health:", 1)[1].splitlines()[0].strip("- ").strip()
    assert line and "not sampled" not in line, line


async def test_critical_flapping_pauses_and_prevents_existing_l1_auto_resume(engine):
    from tradebot.core.models import KillLevel
    from tradebot.health import Check

    engine.killswitch.trigger(KillLevel.L1, trigger="stale_feeds", reason="test")
    for step in range(1, 7):
        engine.health.report(
            "feed", now=START + timedelta(seconds=step), ticks=Check(readiness=step % 2 == 0)
        )
    now = START + timedelta(seconds=7)
    assert engine._hold_on_flapping(now=now) == ["feed"]
    engine._resume(reason="feed recovered", now=now, expected_state=engine.killswitch.state)
    assert engine.killswitch.level is KillLevel.L1


async def test_flapping_raises_l1_and_optional_component_does_not(engine):
    from tradebot.core.models import KillLevel
    from tradebot.health import Check

    engine.health.register("optional-test", critical=False)
    for step in range(1, 7):
        engine.health.report(
            "optional-test", now=START + timedelta(seconds=step), ticks=Check(readiness=step % 2 == 0)
        )
    assert engine._hold_on_flapping(now=START + timedelta(seconds=7)) == []
    assert engine.killswitch.level is KillLevel.L0
    for step in range(1, 7):
        engine.health.report(
            "feed", now=START + timedelta(seconds=step), ticks=Check(readiness=step % 2 == 0)
        )
    assert engine._hold_on_flapping(now=START + timedelta(seconds=7)) == ["feed"]
    assert engine.killswitch.level is KillLevel.L1
    assert engine.autonomy.recent()[-1]["action"] == "hold_for_flapping"


async def test_flapping_hold_survives_restart_and_blocks_manual_and_auto_resume(engine):
    from tradebot.core.models import KillLevel
    from tradebot.health import Check
    from tradebot.risk.killswitch import KillSwitch

    for step in range(1, 7):
        engine.health.report(
            "feed", now=START + timedelta(seconds=step), ticks=Check(readiness=step % 2 == 0)
        )
    engine.clock.set(START + timedelta(seconds=7))
    engine._hold_on_flapping(now=engine.clock.now())
    restarted_switch = KillSwitch(engine.store, clock=engine.clock, kill_file=None)
    assert restarted_switch.level is KillLevel.L1
    assert not restarted_switch.auto_resume(reason="restart")
    with pytest.raises(PermissionError, match="flapping"):
        restarted_switch.reset(reason="operator")
    # Six transitions are no longer in the window after the first ages out.
    engine.clock.set(START + timedelta(minutes=10, seconds=2))
    restarted_switch.reset(reason="settled")
    assert restarted_switch.level is KillLevel.L0


async def test_corrupt_flapping_hold_fails_closed(engine):
    from tradebot.core.models import KillLevel
    from tradebot.risk.killswitch import KillSwitch

    engine.killswitch.trigger(KillLevel.L1, trigger="stale_feeds", reason="test")
    engine.store.kv_set("health_flapping_hold", {"until": "not a timestamp"})
    restarted_switch = KillSwitch(engine.store, clock=engine.clock, kill_file=None)
    assert not restarted_switch.auto_resume(reason="test")
    with pytest.raises(PermissionError):
        restarted_switch.reset(reason="test")


async def test_engine_restores_five_changes_so_restart_cannot_hide_the_sixth(fast_cfg):
    from tradebot.core.models import KillLevel
    from tradebot.health import Check

    fast_cfg.ensure_dirs()
    clock = SimClock(START + timedelta(seconds=6))
    store = Store(":memory:")
    for step in range(1, 6):
        store.log_event(
            "health_change",
            {"component": "feed", "from": "HEALTHY", "to": "DEGRADED" if step % 2 else "HEALTHY"},
            ts=START + timedelta(seconds=step),
        )
    engine = await Engine.build(fast_cfg, clock=clock, store=store)
    try:
        assert len(engine.health.components["feed"].transitions) == 5
        engine.health.report("feed", now=clock.now(), ticks=Check(readiness=True))
        assert engine._hold_on_flapping(now=clock.now()) == ["feed"]
        assert engine.killswitch.level is KillLevel.L1
    finally:
        engine.close()


async def test_engine_restores_six_changes_and_holds_immediately(fast_cfg):
    from tradebot.core.models import KillLevel

    fast_cfg.ensure_dirs()
    clock = SimClock(START + timedelta(seconds=7))
    store = Store(":memory:")
    for step in range(1, 7):
        store.log_event(
            "health_change", {"component": "feed", "from": "HEALTHY", "to": "DEGRADED"},
            ts=START + timedelta(seconds=step),
        )
    engine = await Engine.build(fast_cfg, clock=clock, store=store)
    try:
        assert engine.killswitch.level is KillLevel.L1
        assert engine.killswitch.flapping_hold_active()
    finally:
        engine.close()


async def test_remote_engine_resume_only_clears_reviewed_l1(engine, monkeypatch):
    engine.killswitch.trigger(KillLevel.L2, trigger="test", reason="pause")
    with pytest.raises(PermissionError, match="only clear L1"):
        await engine.resume("reviewed", actor="dashboard", l1_only=True)
    assert engine.killswitch.level is KillLevel.L2
    engine.killswitch.reset(reason="local review")
    engine.killswitch.trigger(KillLevel.L1, trigger="test", reason="pause")
    original = engine.killswitch.reset

    def race(*args, **kwargs):
        engine.killswitch.trigger(KillLevel.L3, trigger="test", reason="concurrent escalation")
        return original(*args, **kwargs)

    monkeypatch.setattr(engine.killswitch, "reset", race)
    with pytest.raises(PermissionError, match="changed"):
        await engine.resume("reviewed", actor="dashboard", l1_only=True)
    assert engine.killswitch.level is KillLevel.L3
