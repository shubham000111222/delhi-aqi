"""The command router must exercise real engine capabilities, not only mocked Ops."""
from datetime import UTC, datetime

from tradebot.alerts.commands import CommandRouter, build_ops
from tradebot.core.clock import SimClock
from tradebot.core.models import KillLevel
from tradebot.core.store import Store
from tradebot.engine import Engine


async def test_pnl_and_report_are_wired_to_real_engine(fast_cfg):
    clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
    engine = await Engine.build(fast_cfg, clock=clock, store=Store(":memory:"))
    router = CommandRouter(build_ops(engine=engine, store=engine.store, clock=clock), frozenset({1}))
    try:
        pnl = router.handle(1, "/pnl")
        assert pnl.ok and "unrealised" in pnl.text and "0.00" in pnl.text
        report = router.handle(1, "/report")
        assert report.ok and "requested" in report.text
        import asyncio

        for _ in range(10):
            await asyncio.sleep(0.02)
            if engine.store.count_events("telegram_action_completed"):
                break
        assert engine.store.count_events("telegram_action_completed") == 1
        assert list(fast_cfg.settings.reports_dir.rglob("report.md"))
    finally:
        engine.close()


async def test_remote_resume_does_not_clear_l2_and_lockdown_reaches_l3(fast_cfg):
    import asyncio

    clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
    engine = await Engine.build(fast_cfg, clock=clock, store=Store(":memory:"))
    router = CommandRouter(build_ops(engine=engine, store=engine.store, clock=clock), frozenset({1}))
    try:
        engine.killswitch.trigger(KillLevel.L2, trigger="test", reason="test")
        reply = router.handle(1, "/resume")
        assert not reply.ok
        assert engine.killswitch.level is KillLevel.L2
        assert router.handle(1, "/lockdown").ok
        assert router.handle(1, "/confirm").ok
        assert engine.killswitch.level is KillLevel.L3
        await asyncio.sleep(0.05)
        assert engine.store.count_events("telegram_action_completed") == 1
    finally:
        engine.close()


async def test_remote_resume_cannot_clear_escalation_after_review(fast_cfg, monkeypatch):
    clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
    engine = await Engine.build(fast_cfg, clock=clock, store=Store(":memory:"))
    router = CommandRouter(build_ops(engine=engine, store=engine.store, clock=clock), frozenset({1}))
    try:
        switch = engine.killswitch
        switch.trigger(KillLevel.L1, trigger="manual", reason="pause")
        original = switch.reset

        def race(*args, **kwargs):
            switch.trigger(KillLevel.L3, trigger="test", reason="concurrent lockdown")
            return original(*args, **kwargs)

        monkeypatch.setattr(switch, "reset", race)
        reply = router.handle(1, "/resume")
        assert not reply.ok
        assert switch.level is KillLevel.L3
        assert engine.store.count_events("manual_reset_refused") == 1
    finally:
        engine.close()
