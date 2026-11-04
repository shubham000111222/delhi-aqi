"""The heal playbook (build plan §20.3, §20.4, §20.6).

A playbook row is not prose. It is four callables and two numbers:

    detect   -> is this fault present right now?
    action   -> do the safest thing that could fix it
    verify   -> did it actually get fixed?
    escalate -> what the ladder does when verify says no
    cooldown -> how often this row may fire
    mttr     -> the §20.6 target, in seconds, so "works" becomes a number

Three rules come straight from §20.1 and are enforced here rather than left to the rows:

* **Healing never adds risk.** A row's action may cancel, reduce, close or protect. It
  cannot open, size up, loosen a limit or change a strategy.
* **Verify before resuming.** An action that is not verified is recorded as a failure and
  counts towards escalation - a repair nobody checked is indistinguishable from a guess.
* **Bounded and logged.** Every attempt writes one event with row, action, outcome and
  duration; repeated failures escalate instead of retrying forever.

The playbook knows nothing about the engine: it receives a context object and two
callbacks. That keeps it testable with a stub, and keeps the trading code out of the part
that decides whether the trading code is well.
"""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from .core.clock import Clock
from .core.logging import get_logger
from .core.models import KillLevel

log = get_logger(__name__)

#: An action may only do these things (§20.1). Declared, not assumed.
SAFE_ACTIONS = frozenset({"cancel", "reduce", "close", "protect", "reconnect", "throttle", "retry", "repair"})


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """A boring breaker: closed → open for `open_seconds` → one probe → closed or open.

    Boring on purpose. A clever breaker is one nobody can debug at 3 a.m., and the thing
    it protects against - hammering a rate-limited endpoint - is not subtle.
    """

    def __init__(self, *, threshold: int = 3, open_seconds: float = 60.0) -> None:
        self.threshold = max(1, int(threshold))
        self.open_seconds = float(open_seconds)
        self.failures = 0
        self.trips = 0
        self.opened_at: datetime | None = None
        self._half_open_probe = False

    def state(self, now: datetime) -> BreakerState:
        if self.opened_at is None:
            return BreakerState.CLOSED
        if (now - self.opened_at).total_seconds() < self.open_seconds:
            return BreakerState.OPEN
        return BreakerState.HALF_OPEN

    def allow(self, now: datetime) -> bool:
        state = self.state(now)
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN:
            return False
        # half-open: exactly one probe goes through
        if self._half_open_probe:
            return False
        self._half_open_probe = True
        return True

    def record_failure(self, now: datetime) -> None:
        self.failures += 1
        self._half_open_probe = False
        if self.failures >= self.threshold:
            self.opened_at = now
            self.trips += 1

    def record_success(self, now: datetime) -> None:
        self.failures = 0
        self.opened_at = None
        self._half_open_probe = False

    def open(self, now: datetime) -> None:
        """Trip it deliberately - the playbook does this on a detected error storm."""
        self.opened_at = now
        self.trips += 1

    def as_dict(self, now: datetime) -> dict[str, Any]:
        return {
            "state": self.state(now).value,
            "failures": self.failures,
            "trips": self.trips,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
        }


@dataclass(frozen=True)
class Row:
    """One line of the playbook."""

    name: str
    detect: Callable[[Any], Awaitable[bool]]
    action: Callable[[Any], Awaitable[str]]
    verify: Callable[[Any], Awaitable[bool]]
    escalate: KillLevel | None = None
    cooldown: timedelta = timedelta(minutes=5)
    mttr_target: float = 10.0
    #: §20.6: a row that is declared but not wired must say so, not silently pass.
    enabled: bool = True
    note: str = ""


@dataclass
class Outcome:
    row: str
    detected_at: datetime
    action: str
    verified: bool
    detail: str
    actioned_at: datetime | None = None
    verified_at: datetime | None = None

    @property
    def mttr_seconds(self) -> float | None:
        if self.actioned_at is None or self.verified_at is None:
            return None
        return (self.verified_at - self.detected_at).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "detected_at": self.detected_at.isoformat(),
            "actioned_at": self.actioned_at.isoformat() if self.actioned_at else None,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "action": self.action,
            "verified": self.verified,
            "detail": self.detail,
            "mttr_seconds": self.mttr_seconds,
        }


class Playbook:
    """Runs the rows, bounds them, and keeps the record a human reads later."""

    def __init__(
        self,
        rows: Sequence[Row],
        *,
        clock: Clock,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        escalate: Callable[[KillLevel, str, str], None] | None = None,
        max_failures: int = 3,
        failure_window: timedelta = timedelta(minutes=10),
    ) -> None:
        self.rows = list(rows)
        self.clock = clock
        self.on_event = on_event
        self._escalate = escalate
        self.max_failures = max(1, int(max_failures))
        self.failure_window = failure_window
        self.last_attempt: dict[str, datetime] = {}
        self.failures: dict[str, deque[datetime]] = defaultdict(deque)
        self.history: list[Outcome] = []
        self.escalations: list[dict[str, Any]] = []
        self.breaker = CircuitBreaker()

    # --- one pass ---------------------------------------------------------
    def _now(self, ctx: Any) -> datetime:
        """The context's clock wins, so a replaced clock is not silently ignored.

        A playbook holding a stale clock reference would keep answering "not yet due"
        forever, which looks exactly like a system with nothing to heal.
        """
        clock = getattr(ctx, "clock", None)
        return clock.now() if clock is not None else self.clock.now()

    async def tick(self, ctx: Any) -> list[Outcome]:
        """Detect, act, verify - for every row that is due. Returns what happened."""
        now = self._now(ctx)
        outcomes: list[Outcome] = []
        for row in self.rows:
            if not row.enabled:
                continue
            last = self.last_attempt.get(row.name)
            if last is not None and now - last < row.cooldown:
                continue
            try:
                present = await row.detect(ctx)
            except Exception as exc:  # noqa: BLE001 - a broken detector must not stop the loop
                self._event("heal_detect_failed", {"row": row.name, "error": str(exc)})
                continue
            if not present:
                continue

            self.last_attempt[row.name] = now
            detected_at = now
            detail = ""
            actioned_at: datetime | None = None
            verified = False
            try:
                detail = await row.action(ctx)
                actioned_at = self._now(ctx)
                verified = await row.verify(ctx)
            except Exception as exc:  # noqa: BLE001 - a failed heal is data, not a crash
                detail = f"action raised: {type(exc).__name__}: {exc}"

            outcome = Outcome(
                row=row.name,
                detected_at=detected_at,
                action=detail,
                verified=verified,
                detail=detail,
                actioned_at=actioned_at,
                verified_at=self._now(ctx) if verified else None,
            )
            self.history.append(outcome)
            if len(self.history) > 500:
                del self.history[: len(self.history) - 500]

            self._event(
                "heal" if verified else "heal_failed",
                {
                    "row": row.name,
                    "action": detail,
                    "verified": verified,
                    "mttr_seconds": outcome.mttr_seconds,
                    "target_seconds": row.mttr_target,
                    "escalate_to": row.escalate.value if row.escalate else None,
                },
                level="INFO" if verified else "WARN",
            )
            if not verified:
                self._record_failure(row, now)
            outcomes.append(outcome)
        return outcomes

    # --- escalation -------------------------------------------------------
    def _record_failure(self, row: Row, now: datetime) -> None:
        window = self.failures[row.name]
        window.append(now)
        while window and now - window[0] > self.failure_window:
            window.popleft()
        if row.escalate is None or len(window) < self.max_failures:
            return
        window.clear()  # escalate once per burst, not once per tick
        entry = {
            "at": now.isoformat(),
            "row": row.name,
            "to": row.escalate.value,
            "reason": f"{self.max_failures} failed repairs in {self.failure_window.total_seconds() / 60:.0f} min",
        }
        self.escalations.append(entry)
        self._event("heal_escalation", entry, level="CRIT")
        if self._escalate is not None:
            self._escalate(row.escalate, f"heal:{row.name}", entry["reason"])

    # --- reporting --------------------------------------------------------
    def _event(self, kind: str, payload: dict[str, Any], *, level: str = "INFO") -> None:
        if self.on_event is not None:
            self.on_event(kind, {**payload, "level": level})

    def metrics(self) -> dict[str, Any]:
        """§20.6: MTTR per row, measured, against its target."""
        by_row: dict[str, list[Outcome]] = defaultdict(list)
        for outcome in self.history:
            by_row[outcome.row].append(outcome)
        targets = {row.name: row.mttr_target for row in self.rows}
        report: dict[str, Any] = {}
        for name, outcomes in sorted(by_row.items()):
            durations = [o.mttr_seconds for o in outcomes if o.mttr_seconds is not None]
            report[name] = {
                "attempts": len(outcomes),
                "verified": sum(1 for o in outcomes if o.verified),
                "p50_seconds": round(statistics.median(durations), 3) if durations else None,
                "p95_seconds": round(sorted(durations)[max(0, int(len(durations) * 0.95) - 1)], 3)
                if durations
                else None,
                "target_seconds": targets.get(name),
                "within_target": bool(durations) and max(durations) <= (targets.get(name) or float("inf")),
            }
        return {
            "rows": [row.name for row in self.rows if row.enabled],
            "unwired": [row.name for row in self.rows if not row.enabled],
            "metrics": report,
            "escalations": self.escalations[-10:],
            "recent": [o.as_dict() for o in self.history[-10:]],
        }

    def as_dict(self, *, now: datetime | None = None) -> dict[str, Any]:
        return {
            "breaker": self.breaker.as_dict(now or self.clock.now()),
            **self.metrics(),
        }
