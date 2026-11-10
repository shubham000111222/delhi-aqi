"""Multi-level kill switch, persisted in SQLite so a restart cannot clear it.

L0 normal | L1 pause entries | L2 flatten everything | L3 lockdown (L2 + manual reset
with a written reason and a 1-hour cooldown).

Escalation is automatic and immediate. De-escalation requires an operator except for
the narrowly scoped L1 recovery paths; every transition is written to the event log.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Mode
from ..core.clock import Clock, iso, parse_iso, utcnow
from ..core.logging import get_logger
from ..core.models import KillLevel
from ..core.store import Store

log = get_logger(__name__)

LOCKDOWN_COOLDOWN = timedelta(hours=1)
# The daily-loss exception is checked separately at the day boundary by the engine.
AUTO_RESUME_TRIGGERS = frozenset(
    {"stale_feeds", "exchange_error_rate", "reconciliation", "disk_full", "daily_loss"}
)


class KillSwitch:
    def __init__(
        self,
        store: Store,
        clock: Clock | None = None,
        kill_file: Path | str | None = Path("KILL"),
        *, mode: Mode = Mode.PAPER,
    ) -> None:
        if not isinstance(mode, Mode):
            raise ValueError("kill switch mode must be a Mode value")
        self._mode = mode
        self.store = store
        self.clock = clock
        self.kill_file = Path(kill_file) if kill_file is not None else None
        self._ensure_state()
        try:
            self.store.recover_pending_kill_incidents()
        except Exception:  # noqa: BLE001 - leave the durable hold intact
            log.exception('{"event":"kill_incident_recovery_failed"}')

    @property
    def mode(self) -> Mode:
        return self._mode

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
        state = self.state
        return (state.get("level") == KillLevel.L0.value
                and not state.get("incident_tracking_failed")
                and not self.store.has_pending_kill_incident()
                and not self.store.has_invalid_ramp_tracking())

    def must_flatten(self) -> bool:
        return self.level in (KillLevel.L2, KillLevel.L3)

    def blocks_reset(self) -> tuple[bool, str]:
        state = self.state
        if state.get("level") != KillLevel.L3.value:
            return False, ""
        raw = state.get("cooldown_until")
        try:
            if not isinstance(raw, str) or not raw:
                raise ValueError("missing or invalid cooldown")
            until = parse_iso(raw)
            return until > self.now(), f"lockdown cooldown until {iso(until)}"
        except (TypeError, ValueError, OverflowError):
            return True, "invalid or missing lockdown cooldown"

    def flapping_hold_active(self) -> bool:
        """A durable pause survives engine restarts and applies to every L0 reset path."""
        hold = self.store.kv_get("health_flapping_hold")
        if hold is None:
            return False
        try:
            return self.now() <= parse_iso(hold["until"])
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
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
        transition_ids: list[int] = []
        tracking_failed = False
        try:
            changed = self.store.set_kill_state(
                level.value, only_escalate=True, track_incident=incident,
                transition_id_out=transition_ids, trigger=trigger, reason=reason, actor=actor,
                cooldown_until=cooldown, ts=self.now(),
            )
        except Exception:  # noqa: BLE001 - retry the protective transition with a latched tracking fault
            log.exception('{"event":"kill_incident_outbox_failed"}')
            tracking_failed = True
            changed = self.store.set_kill_state(
                level.value, only_escalate=True, incident_tracking_failed=True,
                trigger=trigger, reason=reason, actor=actor, cooldown_until=cooldown, ts=self.now(),
            )
        if not changed:
            return False
        if incident and not tracking_failed:
            try:
                self.store.add_incident(
                    level.value, trigger, reason, {"previous_level": current.value, "actor": actor},
                    ts=self.now(), kill_generation=transition_ids[0],
                )
            except Exception:  # noqa: BLE001 - keep protective action running; outbox blocks resume
                log.exception('{"event":"kill_incident_record_failed","generation":%d}', transition_ids[0])
        log.warning(
            '{"event":"kill_switch","from":"%s","to":"%s","trigger":"%s","reason":"%s"}',
            current.value,
            level.value,
            trigger,
            reason,
        )
        return True

    def auto_resume(
        self, *, reason: str, expected_state: dict[str, Any] | None = None, actor: str = "engine"
    ) -> bool:
        """Narrow L1 exception; engine must supply the exact pause it evaluated.

        This boundary checks the cause and transition identity, not the quality of the
        health evidence. The engine remains responsible for verifying recovery. Direct
        store writes and untrusted callers are not an authenticated authorization boundary.
        """
        def refuse(why: str) -> bool:
            self.store.log_event(
                "autonomy_refused", {"actor": actor, "action": "auto_resume", "reason": why},
                level="WARN", actor=actor, ts=self.now(),
            )
            return False

        if self.mode is not Mode.PAPER:
            return refuse("live/testnet auto-resume requires independent recovery evidence; not implemented")
        if expected_state is None:
            return refuse("missing evaluated pause state")
        # A kill file is an independent stop request; honor it before attempting L0.
        if self.check_kill_file():
            return refuse("KILL file present")
        if expected_state.get("level") != KillLevel.L1.value:
            return refuse("only L1 can auto-resume")
        if expected_state.get("trigger") not in AUTO_RESUME_TRIGGERS:
            return refuse("pause cause is not eligible for auto-resume")
        if self.flapping_hold_active():
            return refuse("critical flapping hold active")
        changed = self.store.set_kill_state(
            KillLevel.L0.value,
            expected_state=expected_state,
            require_no_flapping_hold=True,
            require_no_pending_incident=True,
            trigger="auto_resume",
            reason=reason,
            actor=actor,
            ts=self.now(),
        )
        if not changed:
            return refuse("pause state or flapping hold changed")
        log.warning('{"event":"kill_switch_auto_resume","reason":"%s","actor":"%s"}', reason, actor)
        return True

    def reset(
        self, *, reason: str, actor: str = "operator", level: KillLevel = KillLevel.L0,
        expected_state: dict[str, Any] | None = None,
    ) -> None:
        """Reset against the reviewed state; caller identity is not authenticated here.

        Remote callers must supply their reviewed snapshot. Filesystem stop requests can
        still race this check; entry placement independently checks the KILL file.
        """
        def refuse(why: str) -> None:
            self.store.log_event(
                "manual_reset_refused", {"actor": actor, "reason": why},
                level="WARN", actor=actor, ts=self.now(),
            )
            raise PermissionError(why)

        if self.check_kill_file():
            refuse("cannot reset while KILL file is present")
        if self.mode is not Mode.PAPER:
            refuse("live/testnet manual reset requires authenticated authorization; not implemented")
        current = self.state
        if expected_state is not None and current != expected_state:
            refuse("kill state changed; recheck before reset")
        if level is KillLevel.L0 and self.flapping_hold_active():
            refuse("cannot resume while a critical component is flapping")
        if current.get("level") == KillLevel.L3.value:
            try:
                raw = current.get("cooldown_until")
                if not isinstance(raw, str) or not raw:
                    raise ValueError("missing or invalid cooldown")
                until = parse_iso(raw)
                blocked = until > self.now()
            except (TypeError, ValueError, OverflowError):
                refuse("cannot reset L3: invalid or missing cooldown")
                return  # unreachable; helps type checkers understand control flow
            if blocked:
                refuse(f"cannot reset L3 yet: lockdown cooldown until {iso(until)}")
            if not reason.strip():
                refuse("L3 reset requires a written reason")
        changed = self.store.set_kill_state(
            level.value,
            expected_state=current,
            require_no_flapping_hold=level is KillLevel.L0,
            require_no_pending_incident=True,
            trigger="manual_reset",
            reason=reason or "manual reset",
            actor=actor,
            ts=self.now(),
        )
        if not changed:
            refuse("kill state, flapping hold or incident tracking changed; recheck before reset")
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
