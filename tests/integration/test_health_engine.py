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
