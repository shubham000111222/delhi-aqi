"""Live-mode guards (section 3). Every gate must pass or the bot refuses to start live.

Failing closed is the whole point: if a check cannot be *verified* (for example the
exchange does not report API-key permissions), it counts as failed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..config import Config, Mode
from ..core.clock import iso, utc, utcnow
from ..core.logging import get_logger
from ..core.models import KillLevel
from ..core.money import D, to_str
from ..core.store import Store

log = get_logger(__name__)


class LiveGuardError(RuntimeError):
    """Raised when live trading is requested but a gate fails."""

    def __init__(self, failures: list[GuardResult]) -> None:
        self.failures = failures
        detail = "; ".join(f"{f.name}: {f.detail}" for f in failures)
        super().__init__(f"live trading refused ({len(failures)} failed gates): {detail}")


@dataclass(frozen=True)
class GuardResult:
    name: str
    passed: bool
    detail: str = ""
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class RampState:
    stage: int = 0
    stage_started: datetime | None = None
    clean_days: int = 0
    critical_incidents: int = 0

    @property
    def multiplier(self) -> Decimal:
        return D("0.10") if self.stage < 1 else D(["0.10", "0.25", "0.50", "1.0"][min(self.stage, 3)])

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "multiplier": to_str(self.multiplier, 4),
            "stage_started": iso(self.stage_started) if self.stage_started else None,
            "clean_days": self.clean_days,
            "critical_incidents": self.critical_incidents,
        }


class RampController:
    """Live ramp is capped until authenticated, action-bound advancement is built."""

    def __init__(self, config: Config, store: Store) -> None:
        self.cfg = config
        self.store = store
        self.guards = config.settings.live_guards

    @staticmethod
    def _state(raw: Any) -> RampState:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("invalid persisted ramp state")
        stage = raw.get("stage", 0)
        clean_days = raw.get("clean_days", 0)
        incidents = raw.get("critical_incidents", 0)
        if any(type(value) is not int for value in (stage, clean_days, incidents)):
            raise ValueError("invalid persisted ramp counters")
        if not 0 <= stage <= 3 or clean_days < 0 or incidents < 0:
            raise ValueError("invalid persisted ramp counters")
        started = raw.get("stage_started")
        try:
            if started is not None and not isinstance(started, str):
                raise ValueError("invalid stage timestamp")
            stage_started = utc(datetime.fromisoformat(started.replace("Z", "+00:00"))) if started else None
        except (ValueError, OverflowError) as exc:
            raise ValueError("invalid ramp stage timestamp") from exc
        return RampState(stage=stage, stage_started=stage_started, clean_days=clean_days,
                         critical_incidents=incidents)

    @classmethod
    def _state_in_transaction(cls, conn: Any) -> RampState:
        row = conn.execute("SELECT value FROM kv WHERE key='ramp'").fetchone()
        if row is None:
            return cls._state(None)
        raw = json.loads(row["value"])
        if raw is None:
            raise ValueError("invalid persisted ramp state")
        return cls._state(raw)

    def load(self) -> RampState:
        rows = self.store._query("SELECT value FROM kv WHERE key='ramp'")
        if not rows:
            return self._state(None)
        raw = json.loads(rows[0]["value"])
        if raw is None:
            raise ValueError("invalid persisted ramp state")
        return self._state(raw)

    def save(self, state: RampState) -> None:
        """Save only a non-escalating, non-clearing state; no approval consumer exists."""
        checked = self._state(state.to_dict())
        refused = ""
        now = utcnow()
        with self.store.transaction() as conn:
            current = self._state_in_transaction(conn)
            if checked.stage > current.stage:
                refused = "live ramp increase needs authenticated action-bound approval"
            elif checked.critical_incidents < current.critical_incidents:
                refused = "critical ramp incidents cannot be cleared automatically"
            elif (checked.stage == current.stage and current.stage_started is not None
                  and checked.stage_started != current.stage_started):
                refused = "ramp start time cannot be rewritten"
            if refused:
                conn.execute(
                    "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                    (iso(now), "WARN", "autonomy_refused", "ramp", json.dumps({
                        "action": "increase_allocation", "reason": refused,
                        "current_stage": current.stage, "requested_stage": checked.stage,
                    })),
                )
            else:
                conn.execute(
                    "INSERT INTO kv(key,value,updated_at) VALUES('ramp',?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (json.dumps(checked.to_dict()), iso(now)),
                )
        if refused:
            raise PermissionError(refused)

    def advance_if_eligible(self, now: datetime | None = None) -> RampState:
        """Record eligibility without increasing exposure; approvals cannot yet authorize it."""
        now = utc(now or utcnow())
        state = self.load()
        if state.stage_started is None:
            state.stage_started = now
            self.save(state)
            return state
        required_days = self.guards.ramp_min_days[min(state.stage, len(self.guards.ramp_min_days) - 1)]
        elapsed = (now - state.stage_started).days
        if elapsed >= required_days and state.critical_incidents == 0 and state.stage < len(
            self.guards.ramp_multipliers
        ) - 1:
            self.store.log_event(
                "autonomy_refused", {"action": "increase_allocation", "stage": state.stage,
                                     "reason": "authenticated approval consumer not implemented"},
                level="WARN", actor="ramp", ts=now,
            )
        return state

    def register_incident(self) -> None:
        # Read/increment/write under one SQLite transaction so concurrent incidents do
        # not get lost, and a stale save cannot clear one.
        now = utcnow()
        with self.store.transaction() as conn:
            state = self._state_in_transaction(conn)
            state.critical_incidents += 1
            conn.execute(
                "INSERT INTO kv(key,value,updated_at) VALUES('ramp',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (json.dumps(state.to_dict()), iso(now)),
            )
            conn.execute(
                "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                (iso(now), "WARN", "ramp_incident", "ramp", json.dumps({
                    "stage": state.stage, "critical_incidents": state.critical_incidents,
                })),
            )

    @staticmethod
    def operator_cap(raw: Any) -> Decimal:
        """Validate a durable operator cap; malformed records fail closed."""
        if not isinstance(raw, dict) or set(raw) != {"cap"} or not isinstance(raw["cap"], str):
            raise ValueError("invalid operator allocation cap")
        try:
            cap = Decimal(raw["cap"])
            if not cap.is_finite() or cap <= 0 or cap > D("0.10"):
                raise ValueError("invalid operator allocation cap")
            return cap
        except ArithmeticError as exc:
            raise ValueError("invalid operator allocation cap") from exc

    def multiplier(self) -> Decimal:
        if self.cfg.mode is not Mode.LIVE:
            return D("1")
        # Existing database stages predate authentication. Do not trust them to raise
        # exposure; cap at the initial 10% until a real authorization path is built.
        self.load()
        if (self.store.has_pending_kill_incident()
                or self.store.kill_state().get("incident_tracking_failed")):
            raise ValueError("live ramp has an unrecorded kill incident")
        if self.store.has_invalid_ramp_tracking():
            raise ValueError("live ramp incident tracking is invalid; operator investigation required")
        if not self.guards.ramp_multipliers:
            raise ValueError("live ramp requires an initial multiplier")
        initial = self.guards.ramp_multipliers[0]
        if not initial.is_finite() or initial <= 0 or initial > 1:
            raise ValueError("invalid initial live ramp multiplier")
        cap_rows = self.store._query("SELECT value FROM kv WHERE key='operator_allocation_cap'")
        cap = self.operator_cap(json.loads(cap_rows[0]["value"])) if cap_rows else D("0.10")
        return min(initial, D("0.10"), cap)


class LiveGuardChecker:
    """Runs every live-mode gate and records a go/no-go row."""

    def __init__(self, config: Config, store: Store) -> None:
        self.cfg = config
        self.store = store
        self.guards = config.settings.live_guards

    async def check(
        self,
        *,
        confirmation: str | None = None,
        expected_capital: Decimal | None = None,
        adapter: Any = None,
        telegram_ok: bool | None = None,
        reconciliation_clean: bool | None = None,
        feed_fresh: bool | None = None,
        clock_drift_seconds: float | None = None,
        now: datetime | None = None,
    ) -> list[GuardResult]:
        now = utc(now or utcnow())
        results: list[GuardResult] = []

        results.append(
            GuardResult(
                "mode_live",
                self.cfg.mode is Mode.LIVE,
                f"config mode is {self.cfg.mode.value}",
            )
        )
        results.append(
            GuardResult(
                "env_switch",
                self.cfg.secrets.live_trading_enabled,
                "LIVE_TRADING_ENABLED is "
                + ("true" if self.cfg.secrets.live_trading_enabled else "not true"),
            )
        )

        # typed confirmation: the capital amount and the word LIVE
        if self.guards.require_confirmation:
            ok, detail = self._check_confirmation(confirmation, expected_capital)
            results.append(GuardResult("typed_confirmation", ok, detail))

        # go/no-go record from the checklist
        results.append(self._check_go_nogo(now))

        # API key hygiene
        results.append(await self._check_api_key(adapter))

        # capital cap present
        cap = self.cfg.settings.max_capital_usdt
        results.append(
            GuardResult(
                "capital_cap",
                cap > 0 and cap <= self.cfg.settings.starting_capital_usdt * 10,
                f"max_capital_usdt={cap} (starting {self.cfg.settings.starting_capital_usdt})",
            )
        )

        # preflight checks
        ks = KillLevel(self.store.kill_level())
        results.append(GuardResult("preflight_killswitch", ks is KillLevel.L0, f"kill level {ks.value}"))
        results.append(
            GuardResult(
                "preflight_feed",
                bool(feed_fresh),
                "feeds fresh" if feed_fresh else "feeds stale or unverified",
            )
        )
        if clock_drift_seconds is None:
            results.append(GuardResult("preflight_clock", False, "clock drift unverified"))
        else:
            drift_ok = abs(clock_drift_seconds) <= self.cfg.settings.data_quality.ntp_max_drift_seconds
            results.append(
                GuardResult("preflight_clock", drift_ok, f"drift {clock_drift_seconds:.3f}s")
            )
        results.append(
            GuardResult(
                "preflight_reconciliation",
                bool(reconciliation_clean) if self.guards.require_reconciliation_clean else True,
                "reconciliation clean" if reconciliation_clean else "reconciliation not clean",
            )
        )
        results.append(
            GuardResult(
                "preflight_telegram",
                telegram_ok is not False,
                "telegram ok" if telegram_ok else "telegram test not confirmed",
                blocking=False,
            )
        )
        results.append(
            GuardResult(
                "database_writable",
                self._db_writable(),
                f"database {self.store.path} writable",
            )
        )

        failed = [r for r in results if r.blocking and not r.passed]
        self.store.log_event(
            "live_guard_check",
            {"passed": not failed, "results": [r.to_dict() for r in results]},
            level="WARN" if failed else "INFO",
            actor="startup",
        )
        self.store.kv_set(
            "go_nogo",
            {
                "ts": iso(now),
                "passed": not failed,
                "gates": [r.to_dict() for r in results],
            },
        )
        return results

    # --- individual gates -------------------------------------------------
    def _check_confirmation(self, confirmation: str | None, expected_capital: Decimal | None) -> tuple[bool, str]:
        if not confirmation:
            return False, "no typed confirmation supplied"
        text = confirmation.strip()
        if "LIVE" not in text.upper():
            return False, "confirmation must contain the word LIVE"
        if expected_capital is None:
            return False, "confirmation must include the capital amount"
        digits = "".join(ch for ch in text if ch.isdigit() or ch == ".")
        try:
            typed = Decimal(digits)
        except Exception:  # noqa: BLE001
            return False, "could not parse the typed capital amount"
        if typed != expected_capital:
            return False, f"typed capital {typed} != configured {expected_capital}"
        return True, "typed confirmation ok"

    def _check_go_nogo(self, now: datetime) -> GuardResult:
        record = self.store.kv_get("go_nogo")
        if not record:
            return GuardResult("go_nogo_record", False, "no go/no-go record in the database")
        if not record.get("passed"):
            return GuardResult("go_nogo_record", False, "last go/no-go record failed")
        try:
            ts = utc(datetime.fromisoformat(str(record["ts"]).replace("Z", "+00:00")))
        except (KeyError, TypeError, ValueError):
            return GuardResult("go_nogo_record", False, "go/no-go timestamp invalid or missing")
        age = now - ts
        fresh = timedelta(0) <= age <= timedelta(days=self.guards.go_nogo_max_age_days)
        return GuardResult(
            "go_nogo_record",
            fresh,
            f"record from {iso(ts)} ({age.days} days old, max {self.guards.go_nogo_max_age_days})",
        )

    async def _check_api_key(self, adapter: Any) -> GuardResult:
        if adapter is None:
            return GuardResult("api_key_permissions", False, "no adapter supplied to verify permissions")
        checker = getattr(adapter, "verify_permissions", None)
        if checker is None:
            return GuardResult(
                "api_key_permissions",
                False,
                "adapter cannot report key permissions (treated as failed)",
            )
        try:
            perms = await checker(require_ip_whitelist=self.guards.require_ip_whitelist)
        except Exception as exc:  # noqa: BLE001
            return GuardResult("api_key_permissions", False, f"permission check failed: {exc}")
        return GuardResult(
            "api_key_permissions",
            bool(perms.get("passed")),
            str(perms.get("detail", "")),
        )

    def _db_writable(self) -> bool:
        try:
            self.store.kv_set("db_write_check", iso(utcnow()))
            return True
        except Exception:  # noqa: BLE001  # pragma: no cover
            return False

    def require(self, results: list[GuardResult]) -> None:
        failed = [r for r in results if r.blocking and not r.passed]
        if failed:
            raise LiveGuardError(failed)
