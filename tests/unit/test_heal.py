"""The playbook machinery: bounded, verified, audited (§20.3, §20.4, §20.6).

The rows themselves are tested against the engine in
`tests/integration/test_heal_engine.py`; here the question is whether the *frame* keeps
its three promises - act once per cooldown, always verify, escalate instead of retrying
forever - because every row inherits them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tradebot.core.clock import SimClock
from tradebot.core.models import KillLevel
from tradebot.heal import BreakerState, CircuitBreaker, Playbook, Row

T0 = datetime(2026, 11, 4, tzinfo=UTC)


class FakeCtx:
    def __init__(self, *, faulty: bool = True, repairable: bool = True) -> None:
        self.faulty = faulty
        self.repairable = repairable
        self.actions = 0
        self.verified = 0


def make_row(
    ctx: FakeCtx,
    *,
    name: str = "row",
    escalate: KillLevel | None = KillLevel.L1,
    cooldown: timedelta = timedelta(minutes=1),
    target: float = 10.0,
    raises: bool = False,
) -> Row:
    async def detect(_ctx: FakeCtx) -> bool:
        return _ctx.faulty

    async def action(_ctx: FakeCtx) -> str:
        _ctx.actions += 1
        if raises:
            raise RuntimeError("repair exploded")
        if _ctx.repairable:
            _ctx.faulty = False
        return "did the safe thing"

    async def verify(_ctx: FakeCtx) -> bool:
        ok = not _ctx.faulty
        _ctx.verified += int(ok)
        return ok

    return Row(
        name=name,
        detect=detect,
        action=action,
        verify=verify,
        escalate=escalate,
        cooldown=cooldown,
        mttr_target=target,
    )


class TestPlaybook:
    async def test_a_healthy_system_runs_nothing(self):
        ctx = FakeCtx(faulty=False)
        clock = SimClock(T0)
        events: list[tuple[str, dict]] = []
        book = Playbook([make_row(ctx)], clock=clock, on_event=lambda kind, payload: events.append((kind, payload)))

        assert await book.tick(ctx) == []
        assert ctx.actions == 0
        assert events == []

    async def test_a_repair_that_verifies_is_logged_with_its_mttr(self):
        ctx = FakeCtx()
        clock = SimClock(T0)
        events: list[tuple[str, dict]] = []
        book = Playbook([make_row(ctx, target=10.0)], clock=clock, on_event=lambda kind, payload: events.append((kind, payload)))

        outcomes = await book.tick(ctx)

        assert len(outcomes) == 1 and outcomes[0].verified is True
        assert outcomes[0].mttr_seconds == 0.0  # a simulated clock does not move between phases
        assert events[0][0] == "heal"
        assert events[0][1]["row"] == "row"
        assert outcomes[0].mttr_seconds <= 10.0

    async def test_the_cooldown_stops_a_row_from_firing_twice_in_a_row(self):
        ctx = FakeCtx()
        clock = SimClock(T0)
        book = Playbook([make_row(ctx, cooldown=timedelta(minutes=5))], clock=clock)

        await book.tick(ctx)
        ctx.faulty = True  # the fault comes back immediately
        assert await book.tick(ctx) == [], "the cooldown must hold the row back"
        assert ctx.actions == 1

        clock.advance(timedelta(minutes=6))
        assert len(await book.tick(ctx)) == 1

    async def test_an_action_that_raises_is_a_failed_heal_not_a_crash(self):
        ctx = FakeCtx(repairable=False)
        clock = SimClock(T0)
        events: list[tuple[str, dict]] = []
        book = Playbook(
            [make_row(ctx, raises=True)],
            clock=clock,
            on_event=lambda kind, payload: events.append((kind, payload)),
        )

        outcomes = await book.tick(ctx)

        assert outcomes[0].verified is False
        assert "repair exploded" in outcomes[0].detail
        assert events[0][0] == "heal_failed"

    async def test_three_failed_repairs_escalate_once_and_not_four_times(self):
        ctx = FakeCtx(repairable=False)
        clock = SimClock(T0)
        escalations: list[tuple[KillLevel, str, str]] = []
        events: list[tuple[str, dict]] = []
        book = Playbook(
            [make_row(ctx, escalate=KillLevel.L1, cooldown=timedelta(seconds=1))],
            clock=clock,
            on_event=lambda kind, payload: events.append((kind, payload)),
            escalate=lambda level, trigger, reason: escalations.append((level, trigger, reason)),
        )

        for _ in range(5):  # three failures escalate, the next two are a new burst
            clock.advance(timedelta(seconds=2))
            await book.tick(ctx)

        assert len(escalations) == 1, "a burst escalates once, not once per attempt"
        assert escalations[0][0] is KillLevel.L1
        assert escalations[0][1] == "heal:row"
        assert [kind for kind, _ in events].count("heal_escalation") == 1

    async def test_a_row_without_an_escalation_target_never_escalates(self):
        ctx = FakeCtx(repairable=False)
        clock = SimClock(T0)
        escalations: list[tuple] = []
        book = Playbook(
            [make_row(ctx, escalate=None, cooldown=timedelta(seconds=1))],
            clock=clock,
            escalate=lambda *args: escalations.append(args),
        )
        for _ in range(5):
            clock.advance(timedelta(seconds=2))
            await book.tick(ctx)
        assert escalations == []

    async def test_a_disabled_row_is_skipped_and_reported_as_unwired(self):
        ctx = FakeCtx()
        clock = SimClock(T0)
        row = Row(
            name="not_wired",
            detect=lambda _ctx: self._never(),
            action=lambda _ctx: self._noop(),
            verify=lambda _ctx: self._never(),
            enabled=False,
        )
        book = Playbook([row], clock=clock)

        assert await book.tick(ctx) == []
        assert book.metrics()["unwired"] == ["not_wired"]
        assert book.metrics()["rows"] == []

    async def test_metrics_compare_mttr_with_the_target(self):
        ctx = FakeCtx(repairable=False)
        clock = SimClock(T0)
        book = Playbook([make_row(ctx, target=10.0, cooldown=timedelta(seconds=1))], clock=clock)

        for _ in range(3):
            clock.advance(timedelta(seconds=2))
            await book.tick(ctx)

        row = book.metrics()["metrics"]["row"]
        assert row["attempts"] == 3
        assert row["verified"] == 0
        assert row["within_target"] is False
        assert row["target_seconds"] == 10.0

    @staticmethod
    async def _never() -> bool:
        return False

    @staticmethod
    async def _noop() -> str:
        return "nothing"


class TestCircuitBreaker:
    def test_it_opens_after_the_threshold_and_blocks_requests(self):
        breaker = CircuitBreaker(threshold=3, open_seconds=60)
        for _ in range(3):
            breaker.record_failure(T0)

        assert breaker.state(T0) is BreakerState.OPEN
        assert breaker.allow(T0) is False

    def test_half_open_lets_exactly_one_probe_through(self):
        breaker = CircuitBreaker(threshold=1, open_seconds=60)
        breaker.record_failure(T0)
        later = T0 + timedelta(seconds=61)

        assert breaker.state(later) is BreakerState.HALF_OPEN
        assert breaker.allow(later) is True
        assert breaker.allow(later) is False, "one probe, not a stampede"

    def test_a_successful_probe_closes_it(self):
        breaker = CircuitBreaker(threshold=1, open_seconds=60)
        breaker.record_failure(T0)
        later = T0 + timedelta(seconds=61)
        breaker.allow(later)
        breaker.record_success(later)

        assert breaker.state(later) is BreakerState.CLOSED
        assert breaker.allow(later) is True
        assert breaker.trips == 1

    def test_a_failed_probe_reopens_it(self):
        breaker = CircuitBreaker(threshold=1, open_seconds=60)
        breaker.record_failure(T0)
        later = T0 + timedelta(seconds=61)
        breaker.allow(later)
        breaker.record_failure(later)

        assert breaker.state(later) is BreakerState.OPEN
        assert breaker.allow(later) is False

    @pytest.mark.parametrize("threshold", [1, 3, 10])
    def test_the_threshold_is_a_floor_of_one(self, threshold):
        breaker = CircuitBreaker(threshold=threshold)
        for _ in range(threshold - 1):
            breaker.record_failure(T0)
        assert breaker.allow(T0) is True
