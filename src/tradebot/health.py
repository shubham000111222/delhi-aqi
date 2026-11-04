"""Component health for the self-healing loop (build plan §20.2, §20.4).

Every component reports three answers - is it running (*liveness*), can it do its job
(*readiness*), is its output sane (*correctness*) - which collapse into one of three
states: ``HEALTHY``, ``DEGRADED``, ``FAILED``. Three states rather than a percentage,
because the heal playbook has to branch: a degraded feed is retried, a failed one is
replaced, and only a human decides anything else.

The model holds **no policy**. What to do about a degraded component belongs to the heal
playbook (§20.3); what to do when a component keeps flapping belongs to the supervisor.
This module only refuses to lie about state.

It imports nothing from the rest of the bot, so the health of the system can be measured
by something that is not part of it - and so a bug in the trading code cannot make its own
health check crash.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


class State(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


#: Ordering used to pick the worst component; a string compare would put HEALTHY last.
_SEVERITY = {State.HEALTHY: 0, State.DEGRADED: 1, State.FAILED: 2}


@dataclass(slots=True)
class Check:
    """One question about one component, and its answer."""

    liveness: bool = True
    readiness: bool = True
    correctness: bool = True
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.liveness and self.readiness and self.correctness

    def as_dict(self) -> dict[str, Any]:
        return {
            "liveness": self.liveness,
            "readiness": self.readiness,
            "correctness": self.correctness,
            "detail": self.detail,
        }


@dataclass
class ComponentHealth:
    name: str
    critical: bool = True
    state: State = State.HEALTHY
    checks: dict[str, Check] = field(default_factory=dict)
    last_ok: datetime | None = None
    #: When this component changed state, most recent last. Bounded so a flapping
    #: component cannot grow the snapshot without limit.
    transitions: deque[datetime] = field(default_factory=lambda: deque(maxlen=64))

    @property
    def score(self) -> float:
        """Fraction of the last report's checks that passed."""
        if not self.checks:
            return 1.0 if self.state is State.HEALTHY else 0.0
        return sum(1 for check in self.checks.values() if check.ok) / len(self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "critical": self.critical,
            "score": round(self.score, 3),
            "last_ok": self.last_ok.isoformat() if self.last_ok else None,
            "checks": {name: check.as_dict() for name, check in sorted(self.checks.items())},
        }


class HealthModel:
    """The registry every component reports into, and the one place health is summarised."""

    def __init__(self) -> None:
        self.components: dict[str, ComponentHealth] = {}
        #: Every state change, in order. The daily report and the audit trail read this.
        self.changes: list[dict[str, Any]] = []

    def register(self, name: str, *, critical: bool = True) -> ComponentHealth:
        component = self.components.get(name)
        if component is None:
            component = ComponentHealth(name=name, critical=critical)
            self.components[name] = component
        return component

    def report(self, name: str, *, now: datetime, **checks: Check) -> ComponentHealth:
        """Record one component's checks. A state *change* is what gets remembered.

        Liveness failure outranks everything: a component that is not running has no
        readiness or correctness worth arguing about.
        """
        component = self.register(name)
        before = component.state
        component.checks = dict(checks)

        if any(not check.liveness for check in checks.values()):
            state = State.FAILED
        elif any(not (check.readiness and check.correctness) for check in checks.values()):
            state = State.DEGRADED
        else:
            state = State.HEALTHY

        component.state = state
        if state is State.HEALTHY:
            component.last_ok = now
        if state is not before:
            component.transitions.append(now)
            self.changes.append(
                {
                    "at": now.isoformat(),
                    "component": name,
                    "from": before.value,
                    "to": state.value,
                    "detail": "; ".join(
                        f"{key}: {check.detail}" for key, check in sorted(checks.items()) if check.detail
                    ),
                }
            )
        return component

    def flapping(
        self,
        *,
        now: datetime,
        within: timedelta = timedelta(minutes=10),
        limit: int = 5,
    ) -> list[str]:
        """Components that changed state more than `limit` times inside `within` (§20.4).

        Five changes in ten minutes is the plan's threshold; the sixth is the alarm.
        """
        out: list[str] = []
        for name, component in self.components.items():
            recent = [ts for ts in component.transitions if now - ts <= within]
            if len(recent) > limit:
                out.append(name)
        return sorted(out)

    def worst(self) -> State:
        """The worst state among *critical* components: an optional one cannot hold the system."""
        worst = State.HEALTHY
        for component in self.components.values():
            if component.critical and _SEVERITY[component.state] > _SEVERITY[worst]:
                worst = component.state
        return worst

    def snapshot(
        self,
        *,
        now: datetime | None = None,
        restart_budget: RestartBudget | None = None,
    ) -> dict[str, Any]:
        """Everything the dashboard, the report and the watchdog read.

        `now` is required to judge flapping - the detector is about recent history, not
        about the current state - so it is passed in rather than assumed.
        """
        return {
            "overall": self.worst().value,
            "score": round(min((c.score for c in self.components.values() if c.critical), default=1.0), 3),
            "components": {name: component.as_dict() for name, component in sorted(self.components.items())},
            "flapping": self.flapping(now=now) if now is not None else [],
            "restarts": restart_budget.usage(now=now) if (restart_budget and now) else {},
            "changes": self.changes[-20:],
        }


class RestartBudget:
    """At most N restarts per component per hour; the next one is refused, not retried.

    A deterministic bug restarts cleanly forever, which looks exactly like a healthy
    system from the inside. The cap is what turns that into a visible FAILED component and
    an escalation instead of a silent loop (§20.4, scenario 25).
    """

    def __init__(self, *, max_per_hour: int = 3) -> None:
        self.max_per_hour = max(1, int(max_per_hour))
        self.attempts: dict[str, deque[datetime]] = defaultdict(deque)

    def _prune(self, name: str, now: datetime) -> deque[datetime]:
        window = self.attempts[name]
        while window and now - window[0] > timedelta(hours=1):
            window.popleft()
        return window

    def allow(self, name: str, *, now: datetime) -> bool:
        return len(self._prune(name, now)) < self.max_per_hour

    def record(self, name: str, *, now: datetime) -> None:
        self._prune(name, now).append(now)

    def used(self, name: str, *, now: datetime) -> int:
        return len(self._prune(name, now))

    def usage(self, *, now: datetime) -> dict[str, int]:
        return {name: self.used(name, now=now) for name in sorted(self.attempts)}
