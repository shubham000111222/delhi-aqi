"""Restarting the bot, and the rules that stop a restart loop (§20.5).

The plan's supervision table asks for a supervisor tree, a restart on a lost heartbeat,
and a flatten *before* the restart if a position has no protective order. The old
watchdog could only escalate a kill level, which tells a human the bot is wedged and
nothing else.

Three things are load-bearing here:

* **A restart is not free.** Three per component per hour (§20.4), with the same restart
  refused afterwards rather than attempted forever. The budget is the supervisor's, not
  the caller's, so no code path can restart "just once more".
* **Flatten first, then start.** A wedged process leaves the venue holding positions whose
  protective orders may never have been placed. Starting a fresh process into that is how
  a supervision win becomes a loss, so the preflight is a precondition: if it fails, the
  restart does not happen.
* **A restart must prove itself.** Starting a process is not evidence that the bot is
  working; the supervisor waits for the heartbeat to come back and escalates when it does
  not. `started` is not `healthy`.

`ProcessSpawner` is a real `subprocess.Popen` wrapper. It is deliberately not responsible
for "restart forever": that belongs to systemd, Docker's restart policy, or whatever is
above this process. A supervisor that dies with the machine it supervises supervises
nothing.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from ..core.clock import Clock
from ..core.logging import get_logger

log = get_logger(__name__)


@dataclass
class RestartBudget:
    """At most N restarts inside a sliding window, per component."""

    max_per_hour: int = 3
    window: timedelta = timedelta(hours=1)
    records: list[datetime] = field(default_factory=list)

    def _trim(self, now: datetime) -> None:
        cutoff = now - self.window
        self.records = [ts for ts in self.records if ts > cutoff]

    def used(self, now: datetime) -> int:
        self._trim(now)
        return len(self.records)

    def allow(self, now: datetime) -> tuple[bool, str]:
        self._trim(now)
        if len(self.records) >= self.max_per_hour:
            return False, (
                f"{len(self.records)} restart(s) in the last "
                f"{int(self.window.total_seconds() // 60)} min (limit {self.max_per_hour})"
            )
        return True, ""

    def record(self, now: datetime) -> None:
        self._trim(now)
        self.records.append(now)

    def as_dict(self, now: datetime) -> dict[str, Any]:
        return {
            "max_per_hour": self.max_per_hour,
            "used": self.used(now),
            "window_minutes": int(self.window.total_seconds() // 60),
        }


class Spawner(Protocol):
    """Whatever starts and stops a bot process. Faked in tests, `Popen` in production."""

    def start(self) -> Any: ...

    def alive(self, handle: Any) -> bool: ...

    def stop(self, handle: Any) -> None: ...


class ProcessSpawner:
    """Starts `python -m tradebot run` as a child of the watchdog.

    This is the smallest honest version of "restart the bot": it works, it is testable
    against a fake process, and it inherits the watchdog's environment and working
    directory. On a real host, put *this* under systemd/Docker so the supervision survives
    a reboot - a child process cannot outlive its parent.
    """

    def __init__(
        self,
        argv: list[str] | None = None,
        cwd: Path | str | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.argv = argv or [sys.executable, "-m", "tradebot", "run"]
        self.cwd = Path(cwd) if cwd else None
        self._popen = popen
        self.starts = 0

    def start(self) -> Any:
        self.starts += 1
        handle = self._popen(self.argv, cwd=str(self.cwd) if self.cwd else None)
        log.info('{"event":"supervisor_spawn","pid":%s}', getattr(handle, "pid", "?"))
        return handle

    def alive(self, handle: Any) -> bool:
        if handle is None:
            return False
        poll = getattr(handle, "poll", None)
        return poll is None or poll() is None

    def stop(self, handle: Any, timeout: float = 5.0) -> None:
        if handle is None:
            return
        try:
            handle.terminate()
            handle.wait(timeout=timeout)
        except Exception:  # noqa: BLE001 - a stopped process is the goal, not the method
            try:
                handle.kill()
            except Exception:  # pragma: no cover - nothing left to try
                log.exception('{"event":"supervisor_stop_failed"}')


@dataclass
class Supervisor:
    """One restart policy instance for one component."""

    spawner: Spawner
    budget: RestartBudget = field(default_factory=RestartBudget)
    verify_seconds: float = 60.0
    poll_seconds: float = 1.0
    clock: Clock = field(default_factory=Clock)
    on_event: Callable[[str, dict[str, Any]], None] | None = None
    escalate: Callable[[str, str], None] | None = None
    handle: Any = None
    restarts: int = 0
    refusals: int = 0
    failures: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    # --- events -----------------------------------------------------------
    def _event(self, kind: str, payload: dict[str, Any]) -> None:
        entry = {"ts": self.clock.now().isoformat(), "kind": kind, **payload}
        self.history.append(entry)
        if len(self.history) > 200:
            del self.history[: len(self.history) - 200]
        log.warning('{"event":"%s","detail":"%s"}', kind, str(payload)[:300])
        if self.on_event is not None:
            try:
                self.on_event(kind, payload)
            except Exception:  # noqa: BLE001 - an audit sink must not break supervision
                log.exception('{"event":"supervisor_event_sink_failed"}')

    def _escalate(self, trigger: str, reason: str) -> None:
        if self.escalate is not None:
            try:
                self.escalate(trigger, reason)
            except Exception:  # noqa: BLE001
                log.exception('{"event":"supervisor_escalation_failed"}')

    # --- the one operation -------------------------------------------------
    async def handle_lost_heartbeat(
        self,
        *,
        heartbeat_ok: Callable[[], Awaitable[bool]],
        preflight: Callable[[], Awaitable[str]] | None = None,
        started: bool = True,
    ) -> dict[str, Any]:
        """Restart the component, but only after the book is safe and the budget allows.

        `heartbeat_ok` answers "is the bot heartbeating right now": `started` proves a
        process exists, it does not prove the bot works.
        """
        now = self.clock.now()
        result: dict[str, Any] = {"at": now.isoformat()}

        if self.handle is not None and self.spawner.alive(self.handle):
            # A live process that is not heartbeating is wedged: it has to go before a new
            # one starts, or two processes trade the same account.
            result["stopped_wedged"] = True
            self.spawner.stop(self.handle)
            if self.spawner.alive(self.handle):
                self.failures += 1
                result.update(action="stop_failed", reason="previous process is still alive")
                self._event("supervisor_stop_failed", {"reason": result["reason"]})
                self._escalate("supervisor_stop_failed", result["reason"])
                return result
            self.handle = None
            self._event("supervisor_stopped_wedged", {"pid": None})

        allowed, why = self.budget.allow(now)
        if not allowed:
            self.refusals += 1
            result.update(action="refused", reason=why)
            self._event("restart_refused", {"reason": why})
            self._escalate(
                "supervisor_restart_budget",
                f"heartbeat lost and {why}; refusing to restart again - human needed",
            )
            return result

        if preflight is not None:
            try:
                detail = await preflight()
            except Exception as exc:  # noqa: BLE001
                self.failures += 1
                result.update(action="preflight_failed", detail=str(exc))
                self._event("restart_blocked_preflight", {"error": str(exc)})
                self._escalate(
                    "supervisor_preflight_failed",
                    f"cannot make the book safe before restarting ({exc}); not starting",
                )
                return result
            result["preflight"] = detail
            self._event("restart_preflight", {"detail": detail})

        if not started:  # pragma: no cover - exercised by callers that only want the decision
            result.update(action="not_started")
            return result

        self.handle = self.spawner.start()
        self.budget.record(now)
        self.restarts += 1
        result.update(action="restarted", restarts=self.restarts, budget=self.budget.as_dict(now))
        self._event(
            "restart",
            {
                "restarts": self.restarts,
                "used_in_window": self.budget.used(now),
                "limit": self.budget.max_per_hour,
            },
        )

        verified = await self._await_heartbeat(heartbeat_ok)
        if verified:
            result["verified"] = True
        else:
            self.failures += 1
            result["verified"] = False
            self._event("restart_failed", {"verify_seconds": self.verify_seconds})
            self._escalate(
                "supervisor_restart_failed",
                f"restarted but no heartbeat within {self.verify_seconds:.0f}s",
            )
        return result

    async def _await_heartbeat(self, heartbeat_ok: Callable[[], Awaitable[bool]]) -> bool:
        waited = 0.0
        while waited <= self.verify_seconds:
            if self.handle is None or not self.spawner.alive(self.handle):
                return False
            try:
                if await heartbeat_ok():
                    return True
            except Exception:  # noqa: BLE001 - a broken probe is not a heartbeat
                log.exception('{"event":"supervisor_heartbeat_probe_failed"}')
            if waited >= self.verify_seconds:
                break
            await self.clock.sleep(self.poll_seconds)
            waited += self.poll_seconds
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "restarts": self.restarts,
            "refusals": self.refusals,
            "failures": self.failures,
            "budget": self.budget.as_dict(self.clock.now()),
            "alive": self.spawner.alive(self.handle) if self.handle is not None else None,
            "history": self.history[-20:],
        }


def describe() -> dict[str, Any]:
    return {
        "component": "supervisor",
        "restarts": "3 per hour, then refused and escalated",
        "order": "flatten-if-unprotected -> stop wedged -> start -> verify heartbeat",
        "cannot": "restart forever, or outlive its own parent process",
    }


__all__ = ["ProcessSpawner", "RestartBudget", "Spawner", "Supervisor", "describe"]
