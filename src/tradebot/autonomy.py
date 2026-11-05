"""Autonomy: what the system may do on its own, and what it may never do (§21.7).

The plan asks for autonomy levels, an autonomy ledger, approvals that carry numbers and
expire, and a forbidden-action test suite. The reason all four exist is one sentence from
§21.7: *the system may be trusted with bounded actions, never with the bounds themselves.*

So this module is deliberately lopsided. It spends very little code on what the bot is
allowed to do (cancel, retry, repair, protect, close) and a lot on the things it must never
do, plus a permanent record of every action either way.

Five levels, from "look" to "rewrite the rules":

    0 OBSERVE      measure, log, decide nothing
    1 ALERT        tell a human
    2 CORRECT      repair without touching exposure (cancel, retry, reconnect, reconcile)
    3 PROTECT      reduce or close exposure, place protective orders
    4 ALLOCATE     change how much capital something gets   -> needs a human
    5 SELF_MODIFY  change the rules the system runs under   -> never, at any level

This ledger is an audit interface, not an authorization boundary. Level 4 is refused
until an authenticated, action-bound approval consumer is implemented; a caller-provided
name is not proof of approval. Level 5 is refused by this interface.

The refusal helpers are not connected to every trading or configuration entry point.
They do not prevent direct mutations elsewhere; enforcement at those boundaries remains
unfinished. The approvals store is likewise not an authenticated operator interface.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum
from typing import Any, NoReturn

from .core.clock import Clock, utcnow
from .core.logging import get_logger

log = get_logger(__name__)


class AutonomyLevel(IntEnum):
    """How much the system is doing by itself. The numbers are the policy."""

    OBSERVE = 0
    ALERT = 1
    CORRECT = 2
    PROTECT = 3
    ALLOCATE = 4
    SELF_MODIFY = 5

    @property
    def autonomous(self) -> bool:
        """May a running process take this action without a human?"""
        return self <= AutonomyLevel.PROTECT

    @property
    def label(self) -> str:
        return {
            AutonomyLevel.OBSERVE: "observe",
            AutonomyLevel.ALERT: "alert",
            AutonomyLevel.CORRECT: "correct",
            AutonomyLevel.PROTECT: "protect",
            AutonomyLevel.ALLOCATE: "allocate",
            AutonomyLevel.SELF_MODIFY: "self-modify",
        }[self]


#: The highest level a process may reach on its own.
AUTONOMOUS_MAX = AutonomyLevel.PROTECT

#: Actions no code path may perform, whatever it thinks it is allowed to do.
FORBIDDEN_ACTIONS: dict[str, str] = {
    "raise_risk_limit": "risk limits are the thing being defended; a process that can raise them has none",
    "widen_stop": "widening a stop turns a planned loss into an unplanned one",
    "increase_position_size": "adding exposure while unhealthy is the wrong direction at every level",
    "re_enable_strategy": "a disabled strategy was disabled for a reason a process does not get to overturn",
    "lower_kill_level": "only a human lowers a kill level; auto-resume is a whitelisted exception, not this",
    "disable_kill_switch": "the switch that stops everything cannot be stopped by what it stops",
    "edit_runtime_config": "config changes need a restart and a reader, not a running process",
    "change_trading_mode": "paper to live is an operator decision with money attached",
    "approve_own_request": "an approval the actor granted itself is not an approval",
    "delete_audit_log": "the record is the point",
    "place_order_without_kill_check": "no order may skip the kill switch",
}


class ForbiddenAction(Exception):
    """Raised by every operation in `FORBIDDEN_ACTIONS`. Catching it is fine; ignoring it is not."""

    def __init__(self, action: str, reason: str | None = None) -> None:
        self.action = action
        self.reason = reason or FORBIDDEN_ACTIONS.get(action, "not permitted")
        super().__init__(f"{action}: {self.reason}")


class ApprovalRequired(Exception):
    """The action is legal, but not on its own."""

    def __init__(self, action: str, level: AutonomyLevel) -> None:
        self.action = action
        self.level = level
        super().__init__(f"{action} needs a human approval at level {int(level)} ({level.label})")


# --- the operations that always refuse -------------------------------------
#
# They take *args/**kwargs so a caller that half-remembers a signature still hits the wall
# rather than a TypeError that hides the real point.


def _refuse(action: str) -> NoReturn:
    raise ForbiddenAction(action)


def raise_risk_limit(*_args: Any, **_kwargs: Any) -> NoReturn:
    _refuse("raise_risk_limit")


def widen_stop(*_args: Any, **_kwargs: Any) -> NoReturn:
    _refuse("widen_stop")


def increase_position_size(*_args: Any, **_kwargs: Any) -> NoReturn:
    _refuse("increase_position_size")


def re_enable_strategy(*_args: Any, **_kwargs: Any) -> NoReturn:
    _refuse("re_enable_strategy")


def lower_kill_level(*_args: Any, **_kwargs: Any) -> NoReturn:
    _refuse("lower_kill_level")


def disable_kill_switch(*_args: Any, **_kwargs: Any) -> NoReturn:
    _refuse("disable_kill_switch")


def edit_runtime_config(*_args: Any, **_kwargs: Any) -> NoReturn:
    _refuse("edit_runtime_config")


def change_trading_mode(*_args: Any, **_kwargs: Any) -> NoReturn:
    _refuse("change_trading_mode")


def approve_own_request(*_args: Any, **_kwargs: Any) -> NoReturn:
    _refuse("approve_own_request")


def delete_audit_log(*_args: Any, **_kwargs: Any) -> NoReturn:
    _refuse("delete_audit_log")


def place_order_without_kill_check(*_args: Any, **_kwargs: Any) -> NoReturn:
    _refuse("place_order_without_kill_check")


FORBIDDEN_FUNCTIONS = {
    "raise_risk_limit": raise_risk_limit,
    "widen_stop": widen_stop,
    "increase_position_size": increase_position_size,
    "re_enable_strategy": re_enable_strategy,
    "lower_kill_level": lower_kill_level,
    "disable_kill_switch": disable_kill_switch,
    "edit_runtime_config": edit_runtime_config,
    "change_trading_mode": change_trading_mode,
    "approve_own_request": approve_own_request,
    "delete_audit_log": delete_audit_log,
    "place_order_without_kill_check": place_order_without_kill_check,
}


@dataclass
class AutonomyAction:
    """One thing the system did, or tried to do, and under what authority."""

    actor: str
    action: str
    level: AutonomyLevel
    detail: str = ""
    approved_by: str | None = None
    outcome: str = "ok"
    ts: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": (self.ts or utcnow()).isoformat(),
            "actor": self.actor,
            "action": self.action,
            "level": int(self.level),
            "level_label": self.level.label,
            "detail": self.detail,
            "approved_by": self.approved_by,
            "outcome": self.outcome,
        }


@dataclass
class ApprovalRequest:
    """A question with numbers attached, and an expiry date."""

    id: str
    actor: str
    action: str
    numbers: dict[str, Any]
    detail: str = ""
    state: str = "pending"  # pending | approved | rejected | expired
    created_at: datetime | None = None
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None

    def expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or utcnow()) >= self.expires_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "actor": self.actor,
            "action": self.action,
            "numbers": self.numbers,
            "detail": self.detail,
            "state": self.state,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decided_by": self.decided_by,
        }


@dataclass
class AutonomyLedger:
    """Record actions sent to this ledger; it does not intercept other code paths."""

    store: Any = None
    clock: Clock | None = None
    refusals: int = field(default=0)

    def now(self) -> datetime:
        return self.clock.now() if self.clock else utcnow()

    # --- recording --------------------------------------------------------
    def record(
        self,
        actor: str,
        action: str,
        level: AutonomyLevel | int,
        detail: str = "",
        *,
        approved_by: str | None = None,
        outcome: str = "ok",
        ts: datetime | None = None,
    ) -> AutonomyAction:
        """Write one action down, or refuse to.

        A forbidden action raises even if the caller claims an approval, and a self-approved
        action raises as the self-approval it is. Level 5 has no path at all.
        """
        if action in FORBIDDEN_ACTIONS:
            self.refusals += 1
            self._audit_refusal(actor, action, f"forbidden: {FORBIDDEN_ACTIONS[action]}")
            raise ForbiddenAction(action)
        if approved_by is not None and approved_by == actor:
            self.refusals += 1
            self._audit_refusal(actor, action, "self-approval")
            raise ForbiddenAction("approve_own_request")

        level = AutonomyLevel(int(level))
        if level is AutonomyLevel.SELF_MODIFY:
            self.refusals += 1
            self._audit_refusal(actor, action, "self-modification has no path")
            raise ForbiddenAction("edit_runtime_config", "level 5 actions are not permitted, ever")
        if level is AutonomyLevel.ALLOCATE:
            self.refusals += 1
            self._audit_refusal(actor, action, "needs approval")
            raise ApprovalRequired(action, level)

        entry = AutonomyAction(
            actor=actor,
            action=action,
            level=level,
            detail=detail[:2000],
            approved_by=approved_by,
            outcome=outcome,
            ts=ts or self.now(),
        )
        self._write(entry)
        return entry

    def _write(self, entry: AutonomyAction) -> None:
        payload = entry.as_dict()
        log.info(
            '{"event":"autonomy","actor":"%s","action":"%s","level":%d,"outcome":"%s"}',
            entry.actor,
            entry.action,
            int(entry.level),
            entry.outcome,
        )
        if self.store is None:
            return
        self.store.add_autonomy(
            ts=payload["ts"],
            actor=entry.actor,
            action=entry.action,
            level=int(entry.level),
            detail=entry.detail,
            approved_by=entry.approved_by,
            outcome=entry.outcome,
        )
        self.store.log_event(
            "autonomy",
            payload,
            level="WARN" if entry.outcome != "ok" else "INFO",
            actor=entry.actor,
        )

    def _audit_refusal(self, actor: str, action: str, reason: str) -> None:
        """A refusal is the most interesting row in the table, so it is always written."""
        log.error(
            '{"event":"autonomy_refused","actor":"%s","action":"%s","reason":"%s"}',
            actor,
            action,
            reason,
        )
        if self.store is not None:
            self.store.log_event(
                "autonomy_refused",
                {"actor": actor, "action": action, "reason": reason},
                level="CRIT",
                actor="autonomy",
            )

    # --- reading ----------------------------------------------------------
    def recent(self, limit: int = 50, actor: str | None = None) -> list[dict[str, Any]]:
        if self.store is None:
            return []
        return self.store.autonomy_actions(limit=limit, actor=actor)

    def summary(self) -> dict[str, Any]:
        """What the system has done to itself, by actor and by level."""
        rows = self.recent(limit=1000)
        by_actor: dict[str, int] = {}
        by_level: dict[str, int] = {}
        refused = self.store.count_events("autonomy_refused") if self.store is not None else self.refusals
        for row in rows:
            by_actor[row["actor"]] = by_actor.get(row["actor"], 0) + 1
            by_level[row["level_label"]] = by_level.get(row["level_label"], 0) + 1
        return {
            "actions": len(rows),
            "by_actor": dict(sorted(by_actor.items())),
            "by_level": dict(sorted(by_level.items())),
            "refused_attempts": refused,
            "max_autonomous_level": int(AUTONOMOUS_MAX),
            "declared_forbidden_not_globally_enforced": sorted(FORBIDDEN_ACTIONS),
            "recent": rows[-10:],
        }


@dataclass
class Approvals:
    """Store approval requests; identity authentication and atomic consumption are not wired.

    The TTL is not decoration: an approval that arrives three days after the market moved is
    an approval for a different trade, so it expires and the action has to be asked for again.
    """

    store: Any = None
    clock: Clock | None = None
    default_ttl: timedelta = timedelta(hours=24)

    def now(self) -> datetime:
        return self.clock.now() if self.clock else utcnow()

    def request(
        self,
        actor: str,
        action: str,
        numbers: dict[str, Any],
        detail: str = "",
        *,
        ttl: timedelta | None = None,
    ) -> ApprovalRequest:
        if action in FORBIDDEN_ACTIONS:
            raise ForbiddenAction(action)
        if (ttl if ttl is not None else self.default_ttl) <= timedelta(0):
            raise ValueError("approval TTL must be positive")
        if not numbers:
            raise ValueError("an approval request without numbers is a request to trust a mood")
        now = self.now()
        req = ApprovalRequest(
            id=f"ap-{secrets.token_hex(16)}",
            actor=actor,
            action=action,
            numbers=numbers,
            detail=detail,
            created_at=now,
            expires_at=now + (ttl or self.default_ttl),
        )
        if self.store is not None:
            self.store.save_approval(req.as_dict())
        log.info('{"event":"approval_requested","id":"%s","action":"%s"}', req.id, action)
        return req

    def approve(self, request_id: str, *, by: str) -> ApprovalRequest:
        if not by.strip():
            raise ValueError("approver is required")
        req = self._load(request_id)
        if req.actor == by:
            self._refuse_self_approval(req, by)
            raise ForbiddenAction("approve_own_request")
        if req.state != "pending":
            raise ValueError(f"approval {request_id} is already {req.state}")
        if req.expired(self.now()):
            self._expire(req)
            raise ValueError(f"approval {request_id} expired at {req.expires_at}")
        now = self.now()
        if self.store is None or not self.store.transition_approval(
            request_id, state="approved", now=now, by=by
        ):
            raise ValueError(f"approval {request_id} changed or expired")
        req = self._load(request_id)
        log.warning('{"event":"approval_granted","id":"%s","by":"%s"}', req.id, by)
        return req

    def reject(self, request_id: str, *, by: str, reason: str = "") -> ApprovalRequest:
        req = self._load(request_id)
        if req.state != "pending":
            raise ValueError(f"approval {request_id} is already {req.state}")
        if not by.strip():
            raise ValueError("approver is required")
        if req.expired(self.now()):
            self._expire(req)
            raise ValueError(f"approval {request_id} expired at {req.expires_at}")
        detail = f"{req.detail} | rejected: {reason}" if reason else req.detail
        if self.store is None or not self.store.transition_approval(
            request_id, state="rejected", now=self.now(), by=by, detail=detail
        ):
            raise ValueError(f"approval {request_id} changed or expired")
        return self._load(request_id)

    def expire(self) -> int:
        """Mark everything past its deadline expired. Returns how many."""
        count = 0
        for req in self.pending():
            if req.expired(self.now()):
                count += int(self._expire(req))
        return count

    def pending(self) -> list[ApprovalRequest]:
        if self.store is None:
            return []
        return [ApprovalRequest(**row) for row in self.store.approvals(state="pending", limit=-1)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "pending": [req.as_dict() for req in self.pending()],
            "default_ttl_hours": self.default_ttl.total_seconds() / 3600,
        }

    # --- internals --------------------------------------------------------
    def _load(self, request_id: str) -> ApprovalRequest:
        if self.store is None:
            raise KeyError(request_id)
        row = self.store.approval(request_id)
        if row is None:
            raise KeyError(request_id)
        return ApprovalRequest(**row)

    def _expire(self, req: ApprovalRequest) -> bool:
        if self.store is not None and self.store.transition_approval(
            req.id, state="expired", now=self.now()
        ):
            self.store.log_event(
                "approval_expired",
                {"id": req.id, "action": req.action, "actor": req.actor},
                level="WARN",
                actor="autonomy",
            )
            return True
        return False

    def _refuse_self_approval(self, req: ApprovalRequest, by: str) -> None:
        if self.store is not None:
            self.store.log_event(
                "autonomy_refused",
                {"actor": by, "action": "approve_own_request", "id": req.id},
                level="CRIT",
                actor="autonomy",
            )


def describe() -> dict[str, Any]:
    return {
        "component": "autonomy",
        "levels": {int(level): level.label for level in AutonomyLevel},
        "autonomous_max": int(AUTONOMOUS_MAX),
        "approval_required_from": int(AutonomyLevel.ALLOCATE),
        "never": sorted(FORBIDDEN_ACTIONS),
    }


__all__ = [
    "AUTONOMOUS_MAX",
    "ApprovalRequest",
    "ApprovalRequired",
    "Approvals",
    "AutonomyAction",
    "AutonomyLedger",
    "AutonomyLevel",
    "FORBIDDEN_ACTIONS",
    "FORBIDDEN_FUNCTIONS",
    "ForbiddenAction",
    "approve_own_request",
    "change_trading_mode",
    "delete_audit_log",
    "describe",
    "disable_kill_switch",
    "edit_runtime_config",
    "increase_position_size",
    "lower_kill_level",
    "place_order_without_kill_check",
    "raise_risk_limit",
    "re_enable_strategy",
    "widen_stop",
]
