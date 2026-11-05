"""The watchdog end to end: what it does now that it can act (§20.5).

Before this, one pass could raise a kill level and write a row into the same SQLite file the
wedged bot writes to. These tests pin the three additions: it tells someone through a
channel that does not depend on Telegram, it restarts within a budget, and the dead man's
switch flattens before anything else happens.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tradebot.alerts.router import AlertRouter, FileChannel
from tradebot.config import Mode
from tradebot.core.clock import SimClock, iso
from tradebot.core.models import Order, OrderStatus, OrderType, Side
from tradebot.core.store import Store
from tradebot.watchdog.deadman import DeadMansSwitch
from tradebot.watchdog.supervisor import RestartBudget, Supervisor
from tradebot.watchdog.watchdog import LiveWatchdogActions, Watchdog

T0 = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
D = Decimal


class FakeRemote:
    remote = True
    configured = True

    def __init__(self, name: str, *, ok: bool = True) -> None:
        self.name = name
        self.ok = ok
        self.calls: list[str] = []

    async def send(self, text: str, *, level: str = "INFO") -> bool:
        self.calls.append(text)
        return self.ok


class FakeRules:
    tick_size = D("0.1")
    lot_step = D("0.00001")
    min_qty = D("0.00001")
    min_notional = D("5")


class FakeAdapter:
    """Just enough venue for `LiveWatchdogActions`: balances, orders, placing and cancelling."""

    quote = "USDT"
    watchdog_snapshot_is_authoritative = True  # this in-memory venue exposes its full book

    def __init__(self, balances: dict[str, Decimal], stops: list[str] | None = None) -> None:
        self.balances = balances
        self.open_orders: list[Order] = [
            Order(
                client_order_id=f"stop-{symbol.split('/')[0]}",
                symbol=symbol,
                side=Side.SELL,
                qty=D("0.01"),
                type=OrderType.STOP_MARKET,
                status=OrderStatus.SUBMITTED,
                reduce_only=True,
                price=D("90"),
                stop_price=D("90"),
            )
            for symbol in (stops or [])
        ]
        self.placed: list[Any] = []
        self.cancelled = 0

    async def get_balances(self) -> dict[str, Decimal]:
        return dict(self.balances)

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self.open_orders if symbol is None or o.symbol == symbol]

    def get_market_rules(self, symbol: str) -> FakeRules:
        return FakeRules()

    async def place_order(self, request: Any) -> Any:
        self.placed.append(request)
        asset = request.symbol.split("/")[0]
        self.balances[asset] = self.balances.get(asset, D("0")) - request.qty
        return request

    async def cancel_all(self) -> int:
        self.cancelled += len(self.open_orders)
        self.open_orders = []
        return self.cancelled


class FakeProcess:
    def __init__(self) -> None:
        self.alive = True


class FakeSpawner:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start(self) -> Any:
        self.starts += 1
        return FakeProcess()

    def alive(self, handle: Any) -> bool:
        return bool(handle is not None and handle.alive)

    def stop(self, handle: Any) -> None:
        self.stops += 1
        handle.alive = False


@pytest.fixture
def live_cfg(fast_cfg):
    fast_cfg.ensure_dirs()
    settings = fast_cfg.settings.model_copy(
        update={
            "mode": Mode.LIVE,
            "watchdog": fast_cfg.settings.watchdog.model_copy(
                update={"heartbeat_file": "heartbeat", "deadman_seconds": 30.0}
            ),
        }
    )
    return replace(fast_cfg, settings=settings)


def build(
    cfg,
    *,
    clock: SimClock,
    store: Store,
    router: AlertRouter,
    actions: Any = None,
    supervisor: Supervisor | None = None,
    beat: Path | None = None,
) -> Watchdog:
    deadman = None
    if beat is not None:
        deadman = DeadMansSwitch(
            heartbeat_path=beat,
            stale_after=timedelta(seconds=cfg.settings.watchdog.deadman_seconds),
            mode=cfg.mode.value,
            clock=clock,
        )
    return Watchdog(
        cfg,
        store,
        kill_file=None,
        actions=actions,
        clock=clock,
        router=router,
        supervisor=supervisor,
        deadman=deadman,
    )


def alert_file(tmp_path: Path) -> Path:
    return tmp_path / "alerts.jsonl"


class TestAlerting:
    async def test_a_lost_heartbeat_reaches_the_disk_and_a_remote(self, fast_cfg, tmp_path: Path):
        store = Store(":memory:")
        telegram = FakeRemote("telegram", ok=True)
        path = alert_file(tmp_path)
        router = AlertRouter(
            channels=[FileChannel(path=path), telegram], queue_path=tmp_path / "q.jsonl", store=store
        )
        watch = build(fast_cfg, clock=SimClock(T0), store=store, router=router)

        result = await watch.act_async()

        assert result["action"] == "heartbeat_lost"
        assert store.kill_level() == "L2"
        assert path.exists() and "No heartbeat" in path.read_text()
        assert telegram.calls and "No heartbeat" in telegram.calls[0]
        assert router.pending() == []

    async def test_when_every_transport_is_down_the_alert_is_still_on_disk_and_queued(
        self, fast_cfg, tmp_path: Path
    ):
        store = Store(":memory:")
        router = AlertRouter(
            channels=[
                FileChannel(path=alert_file(tmp_path)),
                FakeRemote("telegram", ok=False),
                FakeRemote("webhook", ok=False),
            ],
            queue_path=tmp_path / "q.jsonl",
            store=store,
        )
        watch = build(fast_cfg, clock=SimClock(T0), store=store, router=router)

        await watch.act_async()

        assert len(router.pending()) == 1
        assert "No heartbeat" in router.pending()[0].text
        assert "No heartbeat" in alert_file(tmp_path).read_text()

    async def test_an_operator_kill_file_is_announced_with_instructions(self, fast_cfg, tmp_path: Path):
        store = Store(":memory:")
        telegram = FakeRemote("telegram", ok=True)
        router = AlertRouter(
            channels=[FileChannel(path=alert_file(tmp_path)), telegram], queue_path=tmp_path / "q.jsonl"
        )
        kill = tmp_path / "KILL"
        kill.write_text("maintenance window")
        watch = Watchdog(fast_cfg, store, kill_file=kill, clock=SimClock(T0), router=router)

        result = await watch.act_async()

        assert result["action"] == "kill_file"
        assert "Remove the file to resume" in telegram.calls[0]

    async def test_a_quiet_pass_retries_the_queue_instead_of_doing_nothing(self, fast_cfg, tmp_path: Path):
        store = Store(":memory:")
        telegram = FakeRemote("telegram", ok=False)
        router = AlertRouter(
            channels=[FileChannel(path=alert_file(tmp_path)), telegram], queue_path=tmp_path / "q.jsonl"
        )
        await router.send("delivered to nobody", level="CRIT")
        assert router.pending()

        store.kv_set("heartbeat", iso(SimClock(T0).now()))  # the bot is alive
        watch = build(fast_cfg, clock=SimClock(T0), store=store, router=router)
        telegram.ok = True

        result = await watch.act_async()

        assert result.get("alerts_flushed") == 1
        assert router.pending() == []


class TestDeadMansSwitch:
    async def test_it_flattens_before_reporting_and_escalates(self, live_cfg, tmp_path: Path):
        store = Store(":memory:")
        beat = tmp_path / "heartbeat"
        _old(beat, T0 - timedelta(minutes=5))
        adapter = FakeAdapter({"USDT": D("1000"), "BTC": D("0.01")})
        telegram = FakeRemote("telegram", ok=True)
        router = AlertRouter(
            channels=[FileChannel(path=alert_file(tmp_path)), telegram], queue_path=tmp_path / "q.jsonl"
        )
        watch = build(
            live_cfg,
            clock=SimClock(T0),
            store=store,
            router=router,
            actions=LiveWatchdogActions(adapter),
            beat=beat,
        )

        result = await watch.act_async()

        assert "deadman" in result
        assert result["flatten"]["positions_sold"] == 1
        assert adapter.placed and adapter.placed[0].symbol == "BTC/USDT"
        assert store.kill_level() == "L2"
        assert "DEAD MAN'S SWITCH" in telegram.calls[0]

    async def test_it_does_not_fire_in_paper_even_with_no_heartbeat_file(self, fast_cfg, tmp_path: Path):
        store = Store(":memory:")
        router = AlertRouter(
            channels=[FileChannel(path=alert_file(tmp_path)), FakeRemote("telegram")],
            queue_path=tmp_path / "q.jsonl",
        )
        watch = build(
            fast_cfg,
            clock=SimClock(T0),
            store=store,
            router=router,
            beat=tmp_path / "never-written",
        )

        result = await watch.act_async()

        assert "deadman" not in result
        assert watch.deadman.state()["armed"] is False
        assert result["action"] == "heartbeat_lost"

    async def test_it_does_not_trip_twice(self, live_cfg, tmp_path: Path):
        store = Store(":memory:")
        beat = tmp_path / "heartbeat"
        _old(beat, T0 - timedelta(minutes=5))
        adapter = FakeAdapter({"USDT": D("1000"), "BTC": D("0.01")})
        router = AlertRouter(
            channels=[FileChannel(path=alert_file(tmp_path)), FakeRemote("telegram")],
            queue_path=tmp_path / "q.jsonl",
        )
        watch = build(
            live_cfg,
            clock=SimClock(T0),
            store=store,
            router=router,
            actions=LiveWatchdogActions(adapter),
            beat=beat,
        )

        first = await watch.act_async()
        second = await watch.act_async()

        assert "deadman" in first
        assert "deadman" not in second, "a second trip would flatten what a human just re-opened"
        assert len(adapter.placed) == 1


class TestRestart:
    async def test_a_lost_heartbeat_restarts_the_bot_and_verifies_it(self, fast_cfg, tmp_path: Path):
        store = Store(":memory:")
        spawner = FakeSpawner()
        supervisor = Supervisor(
            spawner=spawner,
            budget=RestartBudget(max_per_hour=3),
            clock=SimClock(T0),
            verify_seconds=5,
            poll_seconds=1,
            escalate=lambda trigger, reason: store.add_incident("L2", trigger, reason),
        )
        router = AlertRouter(
            channels=[FileChannel(path=alert_file(tmp_path)), FakeRemote("telegram")],
            queue_path=tmp_path / "q.jsonl",
        )
        watch = build(
            fast_cfg, clock=SimClock(T0), store=store, router=router, supervisor=supervisor
        )

        result = await watch.act_async()

        assert result["restart"]["action"] == "restarted"
        assert spawner.starts == 1
        assert store.kill_level() == "L2", "the kill level still rises: a restart is not a diagnosis"

    async def test_the_preflight_says_what_it_found_before_starting(self, live_cfg, tmp_path: Path):
        store = Store(":memory:")
        adapter = FakeAdapter({"USDT": D("1000"), "BTC": D("0.01")})
        spawner = FakeSpawner()
        supervisor = Supervisor(
            spawner=spawner,
            clock=SimClock(T0),
            verify_seconds=3,
            poll_seconds=1,
        )
        router = AlertRouter(
            channels=[FileChannel(path=alert_file(tmp_path)), FakeRemote("telegram")],
            queue_path=tmp_path / "q.jsonl",
        )
        # a fresh heartbeat file (so the dead man's switch stays quiet) but a stale store row
        beat = tmp_path / "heartbeat"
        _old(beat, T0)
        watch = build(
            live_cfg,
            clock=SimClock(T0),
            store=store,
            router=router,
            actions=LiveWatchdogActions(adapter),
            supervisor=supervisor,
            beat=beat,
        )

        result = await watch.act_async()

        restart = result["restart"]
        assert restart["action"] == "preflight_failed"
        assert "independent venue snapshot required" in restart["detail"]
        assert not adapter.placed
        assert spawner.starts == 0

    async def test_a_refused_restart_tells_a_human_instead_of_retrying(self, fast_cfg, tmp_path: Path):
        store = Store(":memory:")
        telegram = FakeRemote("telegram", ok=True)
        supervisor = Supervisor(
            spawner=FakeSpawner(),
            budget=RestartBudget(max_per_hour=0),  # everything is already spent
            clock=SimClock(T0),
        )
        router = AlertRouter(
            channels=[FileChannel(path=alert_file(tmp_path)), telegram], queue_path=tmp_path / "q.jsonl"
        )
        watch = build(
            fast_cfg, clock=SimClock(T0), store=store, router=router, supervisor=supervisor
        )

        result = await watch.act_async()

        assert result["restart"]["action"] == "refused"
        assert any("will not restart" in text for text in telegram.calls)


class TestPreflight:
    async def test_a_position_with_a_stop_is_left_alone(self):
        adapter = FakeAdapter({"USDT": D("1000"), "BTC": D("0.01")}, stops=["BTC/USDT"])
        actions = LiveWatchdogActions(adapter)

        assert await actions.unprotected() == []
        assert "no unprotected position" in await actions.flatten_unprotected()
        assert adapter.placed == []

    async def test_only_the_naked_position_is_flattened(self):
        adapter = FakeAdapter({"USDT": D("1000"), "BTC": D("0.01"), "ETH": D("0.5")}, stops=["BTC/USDT"])

        detail = await LiveWatchdogActions(adapter).flatten_unprotected()

        assert [order.symbol for order in adapter.placed] == ["ETH/USDT"]
        assert "ETH/USDT" in detail
        assert "BTC/USDT" not in detail

    async def test_a_quote_balance_is_not_a_position(self):
        adapter = FakeAdapter({"USDT": D("1000")})
        assert await LiveWatchdogActions(adapter).unprotected() == []


def _old(path: Path, ts: datetime) -> None:
    path.write_text(ts.isoformat(), encoding="utf-8")
    stamp = ts.timestamp()
    os.utime(path, (stamp, stamp))


async def test_short_or_wrong_side_stop_is_not_treated_as_protection():
    adapter = FakeAdapter({"USDT": D("1000"), "BTC": D("0.02")}, stops=["BTC/USDT"])
    actions = LiveWatchdogActions(adapter)
    assert await actions.unprotected() == ["BTC/USDT"]
    with pytest.raises(RuntimeError, match="existing sell orders"):
        await actions.flatten_unprotected()
    assert adapter.placed == []
    adapter.open_orders[0].side = Side.BUY
    assert await actions.unprotected() == ["BTC/USDT"]


async def test_live_preflight_fails_when_no_venue_client(live_cfg, tmp_path):
    store = Store(":memory:")
    router = AlertRouter(channels=[FileChannel(path=alert_file(tmp_path))], queue_path=tmp_path / "q")
    watch = build(live_cfg, clock=SimClock(T0), store=store, router=router)
    with pytest.raises(RuntimeError, match="independent venue snapshot"):
        await watch._preflight()
    store.close()


async def test_unverified_flatten_or_uncertified_snapshot_fails_closed():
    adapter = FakeAdapter({"USDT": D("1000"), "BTC": D("0.01")})
    adapter.watchdog_snapshot_is_authoritative = False
    with pytest.raises(RuntimeError, match="cannot verify"):
        await LiveWatchdogActions(adapter).flatten()
    adapter.watchdog_snapshot_is_authoritative = True

    async def no_fill(request):
        return request

    adapter.place_order = no_fill
    with pytest.raises(RuntimeError, match="not verified"):
        await LiveWatchdogActions(adapter).flatten()


async def test_old_fresh_heartbeat_file_does_not_verify_new_process(fast_cfg, tmp_path):
    store = Store(":memory:")
    beat = tmp_path / "heartbeat"
    _old(beat, T0)
    spawner = FakeSpawner()
    supervisor = Supervisor(spawner=spawner, clock=SimClock(T0), verify_seconds=1, poll_seconds=1)
    router = AlertRouter(channels=[FileChannel(path=alert_file(tmp_path))], queue_path=tmp_path / "q")
    watch = build(fast_cfg, clock=SimClock(T0), store=store, router=router, supervisor=supervisor, beat=beat)
    result = await watch.act_async()
    assert result["restart"]["action"] == "restarted"
    assert result["restart"]["verified"] is False
    store.close()


async def test_supervisor_does_not_start_when_old_process_wont_stop():
    class StuckSpawner(FakeSpawner):
        def stop(self, handle):
            self.stops += 1

    spawner = StuckSpawner()
    supervisor = Supervisor(spawner=spawner, clock=SimClock(T0), handle=FakeProcess())

    async def heartbeat():
        return True

    result = await supervisor.handle_lost_heartbeat(heartbeat_ok=heartbeat)
    assert result["action"] == "stop_failed"
    assert spawner.starts == 0
    assert supervisor.handle.alive


async def test_changed_stale_db_pulse_cannot_validate_unchanged_fresh_file(fast_cfg, tmp_path):
    store = Store(":memory:")
    beat = tmp_path / "heartbeat"
    _old(beat, T0)

    class StaleWriter(FakeSpawner):
        def start(self):
            store.kv_set("heartbeat", iso(T0 - timedelta(hours=1)))
            return super().start()

    supervisor = Supervisor(spawner=StaleWriter(), clock=SimClock(T0), verify_seconds=1, poll_seconds=1)
    router = AlertRouter(channels=[FileChannel(path=alert_file(tmp_path))], queue_path=tmp_path / "q")
    watch = build(fast_cfg, clock=SimClock(T0), store=store, router=router, supervisor=supervisor, beat=beat)
    result = await watch.act_async()
    assert result["restart"]["verified"] is False
    store.close()
