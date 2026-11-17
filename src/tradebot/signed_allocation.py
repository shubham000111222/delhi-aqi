"""Offline signed, single-use reduction of the live allocation cap.

The operator key proves key possession, not human identity. This cannot increase a cap,
clear incidents, change the kill switch or certify live readiness.
"""
from __future__ import annotations

import base64
import binascii
import json
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from cryptography.exceptions import InvalidSignature

from .config import Mode
from .core.clock import iso, parse_iso
from .risk.live_guards import RampController
from .signed_recovery import MAX_TTL, SignedFaultRecovery, canonical_bytes

ACTION = "reduce_live_allocation_cap"
FIELDS = frozenset({"version", "request_id", "action", "database_id", "mode", "key_id",
                    "previous_cap", "new_cap", "reason", "issued_at", "expires_at"})


def _cap(value: Any) -> Decimal:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid allocation cap")
    try:
        result = Decimal(value)
        if not result.is_finite() or result <= 0 or result > Decimal("0.10"):
            raise ValueError("invalid allocation cap")
        return result
    except InvalidOperation as exc:
        raise ValueError("invalid allocation cap") from exc


class SignedAllocation(SignedFaultRecovery):
    """A distinct action and payload prevent reuse of other signatures."""

    def _current_cap(self, conn: Any) -> Decimal:
        row = conn.execute("SELECT value FROM kv WHERE key='operator_allocation_cap'").fetchone()
        return RampController.operator_cap(json.loads(row["value"])) if row else Decimal("0.10")

    def request(self, fingerprint: str, reason: str, new_cap: str, *, ttl: timedelta = MAX_TTL) -> dict[str, Any]:  # type: ignore[override]
        if self._mode != Mode.LIVE.value or self.config.mode.value != self._mode:
            raise PermissionError("allocation cap request requires live mode")
        self._public_key(fingerprint)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValueError("written reason required (maximum 500 characters)")
        if ttl <= timedelta(0) or ttl > MAX_TTL:
            raise ValueError("allocation approval TTL must be between zero and 15 minutes")
        proposed = _cap(new_cap)
        with self.store.transaction() as conn:
            previous = self._current_cap(conn)
        if proposed >= previous:
            raise PermissionError("allocation cap can only be reduced")
        now = self._now()
        return {"version": 1, "request_id": secrets.token_hex(32), "action": ACTION,
                "database_id": self._database_id(), "mode": self._mode, "key_id": fingerprint,
                "previous_cap": str(previous), "new_cap": str(proposed), "reason": reason.strip(),
                "issued_at": iso(now), "expires_at": iso(now + ttl)}

    def apply(self, request: dict[str, Any], signature_b64: str) -> None:
        if (not isinstance(request, dict) or set(request) != FIELDS or type(request.get("version")) is not int
                or request["version"] != 1 or any(not isinstance(request[k], str) for k in FIELDS - {"version"})):
            raise ValueError("invalid signed allocation request")
        if request["action"] != ACTION or request["mode"] != Mode.LIVE.value or self._mode != Mode.LIVE.value:
            raise PermissionError("allocation action or mode mismatch")
        if len(request["request_id"]) != 64 or not request["reason"].strip() or len(request["reason"]) > 500:
            raise ValueError("invalid signed allocation values")
        previous, proposed = _cap(request["previous_cap"]), _cap(request["new_cap"])
        if proposed >= previous:
            raise PermissionError("allocation cap can only be reduced")
        try:
            issued, expires = parse_iso(request["issued_at"]), parse_iso(request["expires_at"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid allocation timestamps") from exc
        if expires <= issued or expires - issued > MAX_TTL:
            raise PermissionError("invalid allocation expiry")
        try:
            signature = base64.b64decode(signature_b64, validate=True)
            self._public_key(request["key_id"]).verify(signature, canonical_bytes(request))
        except (InvalidSignature, binascii.Error, TypeError, ValueError) as exc:
            raise PermissionError("invalid operator signature") from exc
        with self.store.transaction() as conn:
            now = self._now()
            if issued > now or now >= expires:
                raise PermissionError("allocation request expired or not yet valid")
            if self.config.mode.value != self._mode:
                raise PermissionError("runtime mode changed")
            try:
                self._public_key(request["key_id"]).verify(signature, canonical_bytes(request))
            except InvalidSignature as exc:
                raise PermissionError("invalid operator signature") from exc
            db = conn.execute("SELECT value FROM meta WHERE key='operator_database_id'").fetchone()
            if db is None or db["value"] != request["database_id"]:
                raise PermissionError("allocation database mismatch")
            if conn.execute("SELECT 1 FROM signed_action_consumptions WHERE request_id=?",
                            (request["request_id"],)).fetchone():
                raise PermissionError("allocation request already consumed")
            if self._current_cap(conn) != previous:
                raise PermissionError("allocation cap changed; approval is stale")
            timestamp = iso(now)
            conn.execute("INSERT INTO kv(key,value,updated_at) VALUES('operator_allocation_cap',?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                         (json.dumps({"cap": str(proposed)}), timestamp))
            conn.execute("INSERT INTO signed_action_consumptions(request_id,ts,action,key_id,payload) VALUES(?,?,?,?,?)",
                         (request["request_id"], timestamp, ACTION, request["key_id"], canonical_bytes(request).decode()))
            conn.execute("INSERT INTO autonomy(ts,actor,action,level,detail,approved_by,outcome) VALUES(?,?,?,?,?,?,?)",
                         (timestamp, "allocation", ACTION, 4, request["reason"], f"key:{request['key_id']}", "ok"))
            conn.execute("INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                         (timestamp, "WARN", "signed_allocation_reduced", f"key:{request['key_id']}",
                          json.dumps({"request_id": request["request_id"], "previous_cap": str(previous),
                                      "new_cap": str(proposed), "reason": request["reason"]})))
