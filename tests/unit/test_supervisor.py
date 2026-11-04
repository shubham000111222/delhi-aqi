"""Restart policy: the budget, the preflight, and proof that the restart worked (§20.4–20.5).

A supervisor that restarts on a lost heartbeat is easy. The three things that make it safe
are all policies, and each has a test here: it stops trying after three an hour, it refuses
to start a new process into an unprotected book, and it does not call a restart successful
until the heartbeat is back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tradebot.core.clock import SimClock
from tradebot.watchdog.supervisor import RestartBudget, Supervisor

T0 = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)


class FakeProcess:
    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.terminated = 0


class FakeSpawner:
    def __init__(self, *, starts_alive: bool = True) -> None:
        self.process = FakeProcess(alive=False)
        self.starts = 0
        self.stops = 0
        self.starts_alive = starts_alive

    def start(self) -> Any:
        self.starts += 1
        self.process = FakeProcess(alive=self.starts_alive)
        return self.process

    def alive(self, handle: Any) -> bool:
        return bool(handle is not None and handle.alive)

    def stop(self, handle: Any) -> None:
        self.stops += 1
        if handle is not None:
            handle.alive = False


def supervisor(**kwargs: Any) -> Supervisor:
    clock = kwargs.pop("clock", SimClock(T0))
    events: list[tuple[str, dict[str, Any]]] = []
    escalations: list[tuple[str, str]] = []

    sup = Supervisor(
        spawner=kwargs.pop("spawner", FakeSpawner()),
        clock=clock,
        on_event=lambda kind, payload: events.append((kind, payload)),
        escalate=lambda trigger, reason: escalations.append((trigger, reason)),
        **kwargs,
    )
    sup.events = events  # type: ignore[attr-defined]
    sup.escalations = escalations  # type: ignore[attr-defined]
    return sup


async def beat_after(*, ok: bool):
    async def heartbeat() -> bool:
        return ok

    return heartbeat


class TestTheHappyPath:
    async def test_it_restarts_and_verifies_against_the_heartbeat(self):
        spawner = FakeSpawner()
        sup = supervisor(spawner=spawner)

        result = await sup.handle_lost_heartbeat(heartbeat_ok=await beat_after(ok=True))

        assert result["action"] == "restarted"
        assert result["verified"] is True
        assert spawner.starts == 1
        assert sup.restarts == 1
        assert [k for k, _ in sup.events] == ["restart"]  # type: ignore[attr-defined]

    async def test_the_preflight_runs_before_the_new_process_starts(self):
        order: list[str] = []

        class OrderedSpawner(FakeSpawner):
            def start(self) -> Any:
                order.append("start")
                return super().start()

        spawner = OrderedSpawner()

        async def preflight() -> str:
            order.append("flatten")
            return "flattened 1 unprotected position(s): BTC/USDT"

        sup = supervisor(spawner=spawner)
        result = await sup.handle_lost_heartbeat(
            heartbeat_ok=await beat_after(ok=True), preflight=preflight
        )

        assert order == ["flatten", "start"], "flattening after the start protects nothing"
        assert result["preflight"] == "flattened 1 unprotected position(s): BTC/USDT"
        assert spawner.starts == 1


class TestRefusals:
    async def test_a_failing_preflight_blocks_the_restart_entirely(self):
        spawner = FakeSpawner()
        sup = supervisor(spawner=spawner)

        async def preflight() -> str:
            raise RuntimeError("venue refused the market order")

        result = await sup.handle_lost_heartbeat(
            heartbeat_ok=await beat_after(ok=True), preflight=preflight
        )

        assert result["action"] == "preflight_failed"
        assert spawner.starts == 0, "starting into an unprotected book is the thing to avoid"
        assert sup.escalations[0][0] == "supervisor_preflight_failed"  # type: ignore[attr-defined]

    async def test_the_fourth_restart_inside_an_hour_is_refused_and_escalated(self):
        clock = SimClock(T0)
        sup = supervisor(clock=clock, budget=RestartBudget(max_per_hour=3))

        for _ in range(3):
            result = await sup.handle_lost_heartbeat(heartbeat_ok=await beat_after(ok=True))
            assert result["action"] == "restarted"
            clock.advance(60)

        refused = await sup.handle_lost_heartbeat(heartbeat_ok=await beat_after(ok=True))

        assert refused["action"] == "refused"
        assert sup.restarts == 3
        assert sup.refusals == 1
        assert sup.escalations[-1][0] == "supervisor_restart_budget"  # type: ignore[attr-defined]
        assert "3 restart(s)" in refused["reason"]

    async def test_the_budget_lets_go_once_the_hour_passes(self):
        clock = SimClock(T0)
        sup = supervisor(clock=clock, budget=RestartBudget(max_per_hour=1))

        await sup.handle_lost_heartbeat(heartbeat_ok=await beat_after(ok=True))
        assert (await sup.handle_lost_heartbeat(heartbeat_ok=await beat_after(ok=True)))[
            "action"
        ] == "refused"

        clock.advance(timedelta(hours=1, seconds=1))
        assert (await sup.handle_lost_heartbeat(heartbeat_ok=await beat_after(ok=True)))[
            "action"
        ] == "restarted"

    async def test_the_budget_is_a_sliding_window_not_a_reset(self):
        budget = RestartBudget(max_per_hour=2)
        budget.record(T0)
        budget.record(T0 + timedelta(minutes=30))

        assert budget.used(T0 + timedelta(minutes=31)) == 2
        assert budget.allow(T0 + timedelta(minutes=31))[0] is False
        assert budget.used(T0 + timedelta(minutes=61)) == 1, "the first restart leaves the window"


class TestWedgedProcesses:
    async def test_a_live_process_with_no_heartbeat_is_stopped_first(self):
        spawner = FakeSpawner()
        sup = supervisor(spawner=spawner)
        sup.handle = spawner.start()  # a process that exists and is not heartbeating

        result = await sup.handle_lost_heartbeat(heartbeat_ok=await beat_after(ok=True))

        assert result["stopped_wedged"] is True
        assert spawner.stops == 1, "two processes trading one account is worse than one wedged one"
        assert spawner.starts == 2  # the initial start plus the restart

    async def test_a_dead_process_is_not_stopped_again(self):
        spawner = FakeSpawner()
        sup = supervisor(spawner=spawner)
        sup.handle = spawner.start()
        sup.handle.alive = False

        result = await sup.handle_lost_heartbeat(heartbeat_ok=await beat_after(ok=True))

        assert "stopped_wedged" not in result
        assert spawner.stops == 0


class TestWhatCountsAsASuccessfulRestart:
    async def test_a_process_that_never_heartbeats_is_a_failure_and_escalates(self):
        clock = SimClock(T0)
        sup = supervisor(clock=clock, verify_seconds=5, poll_seconds=1)

        result = await sup.handle_lost_heartbeat(heartbeat_ok=await beat_after(ok=False))

        assert result["verified"] is False
        assert sup.failures == 1
        assert sup.escalations[-1] == (  # type: ignore[attr-defined]
            "supervisor_restart_failed",
            "restarted but no heartbeat within 5s",
        )

    async def test_it_waits_the_configured_time_before_giving_up(self):
        clock = SimClock(T0)
        probes: list[datetime] = []

        async def heartbeat() -> bool:
            probes.append(clock.now())
            return False

        sup = supervisor(clock=clock, verify_seconds=4, poll_seconds=1)
        await sup.handle_lost_heartbeat(heartbeat_ok=heartbeat)

        assert len(probes) >= 4
        assert (probes[-1] - probes[0]).total_seconds() >= 3

    async def test_a_broken_probe_is_not_a_heartbeat(self):
        calls = {"n": 0}

        async def heartbeat() -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("store is locked")
            return True

        sup = supervisor(verify_seconds=3, poll_seconds=1)
        result = await sup.handle_lost_heartbeat(heartbeat_ok=heartbeat)

        assert result["verified"] is True, "a transient probe failure must not fail the restart"

    async def test_an_event_sink_that_raises_does_not_break_supervision(self):
        sup = Supervisor(
            spawner=FakeSpawner(),
            clock=SimClock(T0),
            on_event=lambda *_: 1 / 0,
        )
        result = await sup.handle_lost_heartbeat(heartbeat_ok=await beat_after(ok=True))
        assert result["action"] == "restarted"


class TestReporting:
    async def test_it_reports_its_budget_and_history(self):
        clock = SimClock(T0)
        sup = supervisor(clock=clock, budget=RestartBudget(max_per_hour=3))
        await sup.handle_lost_heartbeat(heartbeat_ok=await beat_after(ok=True))
        await sup.handle_lost_heartbeat(heartbeat_ok=await beat_after(ok=True))

        summary = sup.as_dict()

        assert summary["restarts"] == 2
        assert summary["budget"] == {"max_per_hour": 3, "used": 2, "window_minutes": 60}
        assert summary["alive"] is True
        kinds = [entry["kind"] for entry in summary["history"]]
        # the second pass stops the first restarted (still unhealthy) process before replacing it
        assert kinds == ["restart", "supervisor_stopped_wedged", "restart"]

    @pytest.mark.parametrize("limit", [1, 3, 10])
    def test_the_budget_never_allows_more_than_its_limit(self, limit: int):
        budget = RestartBudget(max_per_hour=limit)
        for i in range(limit):
            allowed, _ = budget.allow(T0)
            assert allowed is True
            budget.record(T0 + timedelta(seconds=i))
        assert budget.allow(T0 + timedelta(seconds=limit + 1))[0] is False
