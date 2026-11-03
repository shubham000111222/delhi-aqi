"""End-to-end: feed -> strategies -> risk -> paper execution -> ledger -> reports.

Runs the *real* engine over the synthetic market with a simulated clock, which is
the same code path (minus the adapter and feed) that paper and live use.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tradebot.core.clock import SimClock
from tradebot.core.models import KillLevel
from tradebot.core.money import D
from tradebot.core.store import Store
from tradebot.engine import Engine

START = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
async def engine(fast_cfg):
    fast_cfg.ensure_dirs()
    clock = SimClock(START)
    store = Store(":memory:")
    engine = await Engine.build(fast_cfg, clock=clock, store=store)
    yield engine
    engine.close()


async def test_engine_produces_trades_and_stays_consistent(engine):
    await engine.run(stop_at=START + timedelta(days=3))
    stats = engine.stats

    assert stats.ticks > 1000
    assert stats.bars > 100
    assert stats.signals > 0, "no signals at all: strategies never warmed up or never fired"
    assert stats.orders > 0, "signals were produced but no order was ever sent"

    # every order that filled produced exactly one fill record
    assert engine.om.stats.fills <= engine.om.stats.submitted
    assert engine.store.stats()["fills"] == engine.om.stats.fills

    marks = engine.marks
    ok, detail = engine.ledger.check_invariant(marks)
    assert ok, detail
    ok, detail = engine.ledger.reconcile_with_adapter(engine.adapter.cash)
    assert ok, detail

    # no internal accounting errors
    assert engine.store.events(limit=50, kinds=["ledger_error"]) == []

    # every closed trade is persisted with an exit reason
    closed = engine.store.closed_positions(limit=100)
    assert all(t["exit_reason"] for t in closed)
    for trade in engine.ledger.closed_trades:
        assert trade.reason in ("stop", "target", "partial (partial)", "time_stop", "flatten", "trail")

    # equity snapshots exist and track the ledger
    curve = engine.store.equity_series(limit=10)
    assert curve
    live = engine.ledger.equity(marks)
    # the last snapshot is taken at the previous bar close, so allow normal drift
    assert abs(curve[-1]["equity"] - live) / live < D("0.02")


async def test_no_position_exceeds_limits(engine):
    await engine.run(stop_at=START + timedelta(days=2))
    rules = engine.cfg.risk
    for pos in engine.ledger.open_positions:
        marks = engine.marks
        assert pos.qty > 0
        assert pos.stop < pos.avg_entry < pos.target
        exposure = engine.ledger.symbol_exposure(pos.symbol, marks)
        assert exposure <= rules.per_symbol_exposure * engine.ledger.equity(marks) + D("1")
    assert len(engine.ledger.open_positions) <= rules.max_open_positions


async def test_kill_switch_flatten_closes_everything(engine):
    await engine.run(stop_at=START + timedelta(days=2))
    assert engine.killswitch.level is KillLevel.L0

    await engine.kill("integration test kill")
    # let queued fills and flatten work land
    await engine.run(max_events=500)

    assert engine.killswitch.level is KillLevel.L2
    assert engine.ledger.open_positions == []
    assert await engine.adapter.get_open_orders() == []
    assert engine.store.kill_level() == "L2"
    assert engine.store.incidents(limit=5)


async def test_pause_blocks_new_entries(engine):
    await engine.pause("integration pause")
    before = engine.stats.orders
    await engine.run(stop_at=START + timedelta(days=2))
    assert engine.stats.orders == before
    assert engine.killswitch.level is KillLevel.L1


async def test_daily_report_is_written(engine, fast_cfg):
    await engine.run(stop_at=START + timedelta(days=2))
    out = await engine.emit_daily_report()
    assert "DAILY REPORT" in out["telegram"]
    from pathlib import Path

    for key in ("markdown", "html", "json", "tax_csv"):
        path = Path(out["paths"][key])
        assert path.exists() and path.stat().st_size > 0
    html = Path(out["paths"]["html"]).read_text()
    assert "Kill switch" in html and "<table" in html


async def test_portfolio_reserve_and_cooldowns_recorded(engine):
    await engine.run(stop_at=START + timedelta(days=3))
    events = {e["kind"] for e in engine.store.events(limit=5000)}
    assert "tax_reserve" in events or not engine.ledger.closed_trades
    if any(t.reason == "stop" and t.pnl < 0 for t in engine.ledger.closed_trades):
        assert "cooldown" in events


async def test_restart_adopts_open_positions(fast_cfg):
    clock = SimClock(START)
    store = Store(":memory:")
    engine = await Engine.build(fast_cfg, clock=clock, store=store)
    await engine.run(stop_at=START + timedelta(days=2))
    open_positions = list(engine.ledger.open_positions)
    if not open_positions:
        pytest.skip("synthetic run ended flat")

    # simulate a crash + restart against the same database
    engine2 = await Engine.build(fast_cfg, clock=SimClock(engine.clock.now()), store=store)
    restored = {(p.symbol, p.strategy) for p in engine2.ledger.open_positions}
    assert {(p.symbol, p.strategy) for p in open_positions} <= restored
    engine.close()
    engine2.close()
