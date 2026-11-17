"""Fault injection per playbook row (§20.6): one test per wired row.

Each test injects the fault the row exists for, ticks the playbook and asserts three
things: the row fired, the action was attempted, and the system either verified the
repair or escalated. A row that fires but does neither is a row that lies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tradebot.core.clock import SimClock
from tradebot.core.models import KillLevel, OrderRequest, OrderType, Position, Side
from tradebot.core.money import D
from tradebot.core.store import Store
from tradebot.engine import Engine

START = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
async def engine(fast_cfg):
    fast_cfg.ensure_dirs()
    built = await Engine.build(fast_cfg, clock=SimClock(START), store=Store(":memory:"))
    yield built
    built.close()


def kinds(engine: Engine) -> list[str]:
    return [row["kind"] for row in engine.store._query("select kind from events")]


async def test_a_healthy_engine_heals_nothing(engine):
    await engine.run(stop_at=START + timedelta(minutes=30))
    assert engine.playbook.history == []
    assert "heal" not in kinds(engine)


async def test_a_stale_feed_that_cannot_be_repaired_escalates_instead_of_looping(engine):
    """The replay feed has no socket to reconnect, so the repair cannot verify.

    That is the honest path this row must take: two failed attempts are logged, the third
    escalates to L1 rather than pretending a reconnect happened.
    """
    await engine.run(stop_at=START + timedelta(minutes=30))
    engine.clock = SimClock(START + timedelta(hours=3))  # every symbol now stale

    for _ in range(3):
        engine.clock.advance(timedelta(seconds=31))  # past the row's cooldown
        await engine.playbook.tick(engine)

    events = kinds(engine)
    assert "heal_failed" in events
    assert "heal_escalation" in events
    assert engine.killswitch.level is KillLevel.L1
    assert engine.playbook.metrics()["metrics"]["feed_stale"]["attempts"] == 3


async def test_a_reconciliation_mismatch_is_repaired_and_verified(engine):
    engine._reconcile_state = "adapter cash 990.00 vs ledger 1000.00"

    outcomes = await engine.playbook.tick(engine)

    reconcile = next(o for o in outcomes if o.row == "reconcile_mismatch")
    assert reconcile.verified is True, "the repair must be verified, not assumed"
    assert engine._reconcile_state == "ok"
    assert "heal" in kinds(engine)


async def test_an_order_the_venue_holds_and_we_do_not_is_cancelled_not_just_reported(engine):
    """The live-run failure that produced this test.

    A five-minute paper session ended with `heal_failed: reconcile_mismatch - unknown open
    orders` and an L1, because the row's repair was to run the comparison again, which
    reported the same disagreement. A comparison is not a repair: the unknown order has to
    be cancelled before the two sides can agree.
    """
    await engine.run(stop_at=START + timedelta(minutes=30))
    symbol = engine.cfg.symbol_names()[0]
    price = engine.market.price(symbol)
    unknown = await engine.adapter.place_order(
        OrderRequest(
            tag="lost_entry",
            symbol=symbol,
            side=Side.BUY,
            qty=D("0.01"),
            type=OrderType.LIMIT,
            price=price * D("0.5"),  # far from the market, so it rests
            strategy="S1_ema_trend",
        )
    )
    engine._reconcile_state = "stale mismatch from the previous tick"

    outcomes = await engine.playbook.tick(engine)

    reconcile = next(o for o in outcomes if o.row == "reconcile_mismatch")
    assert reconcile.verified is True
    assert "cancelling 1 unknown order" in reconcile.detail
    still_open = await engine.adapter.get_open_orders(symbol)
    assert all(o.client_order_id != unknown.client_order_id for o in still_open)


async def test_the_disk_row_is_due_only_above_the_warning_threshold(engine):
    """A row whose detector never fires must not appear as a heal attempt."""
    await engine.run(stop_at=START + timedelta(minutes=10))
    outcomes = await engine.playbook.tick(engine)
    assert all(outcome.row != "disk_pressure" for outcome in outcomes)


async def test_paper_mode_never_places_venue_stops(engine):
    """§20.3's unprotected-position row is a live/testnet rule.

    In paper the protective stop is the local monitor; firing a "place the exchange stop"
    heal there would be a repair that cannot exist.
    """
    assert engine.cfg.mode.value == "paper"
    row = next(r for r in engine.playbook.rows if r.name == "position_unprotected")
    assert await row.detect(engine) is False


async def test_the_stuck_exit_row_sees_a_position_that_outlived_its_exit(engine):
    """Inject the state the row exists for: an exit submitted long ago, position still open.

    A market exit that never closes its position is the one stuck order that matters -
    the position stays exposed while the bot believes it is leaving.
    """
    await engine.run(stop_at=START + timedelta(minutes=30))
    symbol = engine.cfg.symbol_names()[0]
    position = Position(
        symbol=symbol,
        strategy="S1_ema_trend",
        qty=D("0.01"),
        avg_entry=D("100"),
        stop=D("95"),
        target=D("110"),
        initial_stop=D("95"),
        opened_at=START,
    )
    engine.ledger.positions[(symbol, "S1_ema_trend")] = position
    base = symbol.split("/")[0]
    engine.adapter.holdings[base] = position.qty  # the venue thinks we hold it, as it would
    engine._exit_attempts[(symbol, "S1_ema_trend")] = engine.clock.now() - timedelta(minutes=5)

    row = next(r for r in engine.playbook.rows if r.name == "exit_stuck")
    assert await row.detect(engine) is True

    # and the action re-submits it; the paper adapter fills a market exit immediately
    detail = await row.action(engine)
    assert "re-submitted" in detail
    assert await row.verify(engine) is True, "the position must be gone or the heal is a failure"


async def test_reconciliation_heal_does_not_change_risk_limits_or_enable_strategy(engine):
    """Exercise a real playbook row rather than only testing a refusal helper."""
    risk_before = engine.cfg.risk.model_dump()
    engine._disabled_strategies["S1_ema_trend"] = "operator disabled"
    engine.killswitch.trigger(KillLevel.L1, trigger="manual", reason="operator pause")
    engine._reconcile_state = "mismatch"

    outcomes = await engine.playbook.tick(engine)

    assert any(outcome.row == "reconcile_mismatch" for outcome in outcomes)
    assert engine.cfg.risk.model_dump() == risk_before
    assert engine._strategy_blocked("S1_ema_trend")
    assert engine.killswitch.level is KillLevel.L1
