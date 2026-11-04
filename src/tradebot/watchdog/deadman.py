"""Dead man's switch: the switch that fires when the bot cannot tell you anything.

A watchdog shares a database with the bot. That is fine until the thing that breaks is the
database, or the machine's disk, or the process's ability to write to either — at which
point a watchdog reading the same store concludes "no heartbeat" only if it can still read
it. The dead man's switch is the cruder, more reliable version: a file the bot refreshes
on every tick, and a separate process that closes positions when the file goes stale.

Design rules, each one the result of how these switches fail in practice:

* **Arma only where money is at risk.** In paper mode there is nothing to close, and a
  switch that fires during a laptop sleep teaches the operator to disable it. It reports
  `armed: false` and does nothing.
* **It latches.** Once tripped it stays tripped until a human resets it, because a switch
  that re-arms itself the moment the heartbeat returns will re-flatten the position a human
  just re-opened.
* **It cannot be reset while the heartbeat is still dead.** That is the state in which
  resetting is a lie.
* **A missing file counts as stale.** A bot that has never started, or whose disk filled,
  is not heartbeating.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.clock import Clock
from ..core.logging import get_logger

log = get_logger(__name__)

LIVE_MODES = ("testnet", "live")


@dataclass
class DeadMansSwitch:
    """File-based liveness, with a latch."""

    heartbeat_path: Path
    stale_after: timedelta = timedelta(seconds=30)
    mode: str = "paper"
    clock: Clock = field(default_factory=Clock)
    tripped: bool = False
    tripped_at: datetime | None = None
    resets: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.heartbeat_path, Path):
            self.heartbeat_path = Path(self.heartbeat_path)

    @property
    def armed(self) -> bool:
        return str(self.mode).lower() in LIVE_MODES

    # --- reading the pulse -------------------------------------------------
    def last_beat(self) -> datetime | None:
        try:
            mtime = self.heartbeat_path.stat().st_mtime
        except OSError:
            return None
        return datetime.fromtimestamp(mtime, tz=self.clock.now().tzinfo)

    def age(self, now: datetime | None = None) -> timedelta | None:
        """None means the file does not exist, which is stale in every useful sense."""
        last = self.last_beat()
        if last is None:
            return None
        return (now or self.clock.now()) - last

    def stale(self, now: datetime | None = None) -> bool:
        age = self.age(now)
        return age is None or age > self.stale_after

    # --- the decision ------------------------------------------------------
    def check(self, now: datetime | None = None) -> bool:
        """True exactly once per trip: the caller's cue to flatten and alert."""
        if not self.armed or self.tripped:
            return False
        moment = now or self.clock.now()
        if not self.stale(moment):
            return False
        self.tripped = True
        self.tripped_at = moment
        age = self.age(moment)
        log.critical(
            '{"event":"deadman_tripped","age_seconds":%s,"limit_seconds":%s,"mode":"%s"}',
            "missing" if age is None else f"{age.total_seconds():.1f}",
            self.stale_after.total_seconds(),
            self.mode,
        )
        return True

    def reset(self, now: datetime | None = None) -> bool:
        """Operator action after investigating. Refused while the heartbeat is still dead."""
        if not self.tripped:
            return False
        if self.stale(now):
            return False
        self.tripped = False
        self.tripped_at = None
        self.resets += 1
        return True

    def state(self, now: datetime | None = None) -> dict[str, Any]:
        age = self.age(now)
        return {
            "armed": self.armed,
            "mode": self.mode,
            "tripped": self.tripped,
            "tripped_at": self.tripped_at.isoformat() if self.tripped_at else None,
            "heartbeat_age_seconds": None if age is None else round(age.total_seconds(), 1),
            "stale_after_seconds": self.stale_after.total_seconds(),
            "path": str(self.heartbeat_path),
            "resets": self.resets,
        }


def describe() -> dict[str, Any]:
    return {
        "component": "deadman",
        "armed_in": list(LIVE_MODES),
        "fires_on": "heartbeat file missing or older than stale_after",
        "latch": "requires an explicit reset, and only while the heartbeat is fresh",
    }


__all__ = ["DeadMansSwitch", "describe"]
