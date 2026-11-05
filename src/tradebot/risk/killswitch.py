"""Multi-level kill switch, persisted in SQLite so a restart cannot clear it.

L0 normal | L1 pause entries | L2 flatten everything | L3 lockdown (L2 + manual reset
with a written reason and a 1-hour cooldown).

Escalation is automatic and immediate. De-escalation never happens by itself: a
human has to reset it, and every transition is written to the append-only event log.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.clock import Clock, iso, parse_iso, utcnow
from ..core.logging import get_logger
from ..core.models import KillLevel
from ..core.store import Store

log = get_logger(__name__)

LOCKDOWN_COOLDOWN = timedelta(hours=1)


class KillSwitch:
    def __init__(
        self,
        store: Store,
        clock: Clock | None = None,
        kill_file: Path | str | None = Path("KILL"),
    ) -> None:
        self.store = store
        self.clock = clock
        self.kill_file = Path(kill_file) if kill_file is not None else None
        self._ensure_state()

    # --- state ------------------------------------------------------------
    def _ensure_state(self) -> None:
        if self.store.kv_get("kill_state") is None:
            self.store.set_kill_state(
                KillLevel.L0.value, trigger="startup", reason="initial state", only_if_missing=True
            )

    def now(self) -> datetime:
        return self.clock.now() if self.clock else utcnow()

    @property
    def state(self) -> dict[str, Any]:
        return self.store.kill_state()

    @property
    def level(self) -> KillLevel:
        # Other processes (notably the watchdog) can escalate at any time. A cached
        # L0 would allow entries after their stop decision, so always read persisted state.
        return KillLevel(self.state.get("level", "L0"))

    @property
    def cooldown_until(self) -> datetime | None:
        raw = self.state.get("cooldown_until")
        if not raw:
            return None
        from ..core.clock import parse_iso

        return parse_iso(raw)

    # --- queries ----------------------------------------------------------
    def can_open_entries(self) -> bool:
        self.check_kill_file()
        return self.level is KillLevel.L0

    def must_flatten(self) -> bool:
        return self.level in (KillLevel.L2, KillLevel.L3)

    def blocks_reset(self) -> tuple[bool, str]:
        if self.level is not KillLevel.L3:
            return False, ""
        until = self.cooldown_until
        if until is None:
            return False, ""
        return until > self.now(), f"lockdown cooldown until {iso(until)}"

    def flapping_hold_active(self) -> bool:
        """A durable pause survives engine restarts and applies to every L0 reset path."""
        hold = self.store.kv_get("health_flapping_hold")
        if hold is None:
            return False
        try:
            return self.now() <= parse_iso(hold["until"])
        except (KeyError, TypeError, ValueError):
            # Corrupt safety state must never quietly authorize trading.
            return True

    # --- transitions ------------------------------------------------------
    def trigger(
        self,
        level: KillLevel,
        *,
        trigger: str,
        reason: str,
        actor: str = "system",
        incident: bool = True,
    ) -> bool:
        """Escalate only. Returns True if the level actually changed."""
        current = self.level
        if level.rank <= current.rank:
            return False
        cooldown = self.now() + LOCKDOWN_COOLDOWN if level is KillLevel.L3 else None
        changed = self.store.set_kill_state(
            level.value,
            only_escalate=True,
            trigger=trigger,
            reason=reason,
            actor=actor,
            cooldown_until=cooldown,
            ts=self.now(),
        )
        if not changed:
            return False
        if incident:
            self.store.add_incident(
                level.value,
                trigger,
                reason,
                {"previous_level": current.value, "actor": actor},
                ts=self.now(),
            )
        log.warning(
            '{"event":"kill_switch","from":"%s","to":"%s","trigger":"%s","reason":"%s"}',
            current.value,
            level.value,
            trigger,
            reason,
        )
        return True

    def auto_resume(self, *, reason: str, actor: str = "engine") -> bool:
        """Return to L0 from L1 only.

        L2 and L3 are never cleared automatically: a flatten or a lockdown is a
        decision a human made (or must review), and healing infrastructure must never
        quietly re-arm trading. This exists so that a *time-boxed* pause (a bad day, a
        recovered feed, a transient API storm) can lift once its condition is gone,
        with the transition recorded like any other.
        """
        current = self.state
        if current.get("level") != KillLevel.L1.value or self.flapping_hold_active():
            return False
        changed = self.store.set_kill_state(
            KillLevel.L0.value,
            expected_state=current,
            require_no_flapping_hold=True,
            trigger="auto_resume",
            reason=reason,
            actor=actor,
            ts=self.now(),
        )
        if not changed:
            return False
        log.warning('{"event":"kill_switch_auto_resume","reason":"%s","actor":"%s"}', reason, actor)
        return True

    def reset(self, *, reason: str, actor: str = "operator", level: KillLevel = KillLevel.L0) -> None:
        """Manual reset. L3 requires a written reason and the cooldown to have expired."""
        current = self.state
        if level is KillLevel.L0 and self.flapping_hold_active():
            raise PermissionError("cannot resume while a critical component is flapping")
        if current.get("level") == KillLevel.L3.value:
            blocked, why = self.blocks_reset()
            if blocked:
                raise PermissionError(f"cannot reset L3 yet: {why}")
            if not reason.strip():
                raise PermissionError("L3 reset requires a written reason")
        changed = self.store.set_kill_state(
            level.value,
            expected_state=current,
            require_no_flapping_hold=level is KillLevel.L0,
            trigger="manual_reset",
            reason=reason or "manual reset",
            actor=actor,
            ts=self.now(),
        )
        if not changed:
            raise PermissionError("kill state or flapping hold changed; recheck before reset")
        self.store.kv_set("last_reset", {"ts": iso(self.now()), "reason": reason, "actor": actor})
        log.warning('{"event":"kill_switch_reset","to":"%s","actor":"%s"}', level.value, actor)

    # --- manual trigger: a KILL file in the project folder -----------------
    def check_kill_file(self) -> bool:
        if self.kill_file is None or not self.kill_file.exists():
            return False
        try:
            reason = self.kill_file.read_text(encoding="utf-8").strip()[:500] or "KILL file present"
        except OSError:  # pragma: no cover
            reason = "KILL file present"
        if self.level is not KillLevel.L3:
            self.trigger(
                KillLevel.L2,
                trigger="kill_file",
                reason=reason,
                actor="kill_file",
            )
        return True

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.kill_history(limit)
