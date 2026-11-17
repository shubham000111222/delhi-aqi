"""Action-bound signed reductions of a strategy's durable sizing multiplier.

This action cannot increase risk, release a hold, reset a kill switch, or prove that
submitted quantities were calculated honestly. Key possession is not human identity.
"""
from __future__ import annotations

import base64
import binascii
import json
import secrets
from datetime import timedelta
from decimal import Decimal
from typing import Any

from cryptography.exceptions import InvalidSignature

from .core.clock import iso, parse_iso
from .core.store import Store
from .signed_recovery import MAX_TTL, SignedFaultRecovery, canonical_bytes

ACTION = "signed_reduce_strategy_size"
FIELDS = frozenset({"version", "request_id", "action", "database_id", "mode", "key_id", "strategy",
                    "previous_multiplier", "new_multiplier", "reason", "issued_at", "expires_at"})


class SignedStrategyReduction(SignedFaultRecovery):
    """Consume a distinct, signed, strictly risk-reducing action once."""

    @staticmethod
    def _current(conn: Any, strategy: str) -> Decimal:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (f"size_mult_{strategy}",)).fetchone()
        return Store._strategy_size_value(json.loads(row["value"])) if row else Decimal("1")

    def _strategy(self, name: Any) -> str:
        if (not isinstance(name, str) or not name or len(name) > 200
                or name not in self.config.enabled_strategies):
            raise ValueError("strategy must be currently enabled in the reviewed configuration")
        return name

    def request(self, fingerprint: str, reason: str, strategy: str, multiplier: str,  # type: ignore[override]
                *, ttl: timedelta = MAX_TTL) -> dict[str, Any]:
        if self.config.mode.value != self._mode:
            raise PermissionError("runtime mode changed")
        self._public_key(fingerprint)
        strategy = self._strategy(strategy)
        proposed = Store._strategy_size_value(multiplier)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValueError("written reason required (maximum 500 characters)")
        if ttl <= timedelta(0) or ttl > MAX_TTL:
            raise ValueError("approval TTL must be between zero and 15 minutes")
        with self.store.transaction() as conn:
            previous = self._current(conn, strategy)
        if proposed >= previous:
            raise PermissionError("strategy multiplier can only be reduced")
        now = self._now()
        return {"version": 1, "request_id": secrets.token_hex(32), "action": ACTION,
                "database_id": self._database_id(), "mode": self._mode, "key_id": fingerprint,
                "strategy": strategy, "previous_multiplier": str(previous), "new_multiplier": str(proposed),
                "reason": reason.strip(), "issued_at": iso(now), "expires_at": iso(now + ttl)}

    def apply(self, request: dict[str, Any], signature_b64: str) -> None:
        if (not isinstance(request, dict) or set(request) != FIELDS or type(request.get("version")) is not int
                or request["version"] != 1 or any(not isinstance(request[k], str) for k in FIELDS - {"version"})):
            raise ValueError("invalid signed strategy request")
        if request["action"] != ACTION or request["mode"] != self._mode or self.config.mode.value != self._mode:
            raise PermissionError("strategy action or mode mismatch")
        if (len(request["request_id"]) != 64 or any(c not in "0123456789abcdef" for c in request["request_id"])
                or not request["reason"].strip() or len(request["reason"]) > 500):
            raise ValueError("invalid signed strategy values")
        strategy = self._strategy(request["strategy"])
        previous = Store._strategy_size_value(request["previous_multiplier"])
        proposed = Store._strategy_size_value(request["new_multiplier"])
        if proposed >= previous:
            raise PermissionError("strategy multiplier can only be reduced")
        try:
            issued, expires = parse_iso(request["issued_at"]), parse_iso(request["expires_at"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid strategy approval timestamps") from exc
        if expires <= issued or expires - issued > MAX_TTL:
            raise PermissionError("invalid strategy approval expiry")
        try:
            signature = base64.b64decode(signature_b64, validate=True)
            self._public_key(request["key_id"]).verify(signature, canonical_bytes(request))
        except (InvalidSignature, binascii.Error, TypeError, ValueError) as exc:
            raise PermissionError("invalid operator signature") from exc
        with self.store.transaction() as conn:
            now = self._now()
            if issued > now or now >= expires:
                raise PermissionError("strategy request expired or not yet valid")
            if self.config.mode.value != self._mode:
                raise PermissionError("runtime mode changed")
            self._strategy(strategy)
            try:
                self._public_key(request["key_id"]).verify(signature, canonical_bytes(request))
            except InvalidSignature as exc:
                raise PermissionError("invalid operator signature") from exc
            db = conn.execute("SELECT value FROM meta WHERE key='operator_database_id'").fetchone()
            if db is None or db["value"] != request["database_id"]:
                raise PermissionError("strategy database mismatch")
            if conn.execute("SELECT 1 FROM signed_action_consumptions WHERE request_id=?",
                            (request["request_id"],)).fetchone():
                raise PermissionError("strategy request already consumed")
            if self._current(conn, strategy) != previous:
                raise PermissionError("strategy multiplier changed; approval is stale")
            timestamp = iso(now)
            conn.execute("INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                         (f"size_mult_{strategy}", json.dumps(str(proposed)), timestamp))
            conn.execute("INSERT INTO signed_action_consumptions(request_id,ts,action,key_id,payload) VALUES(?,?,?,?,?)",
                         (request["request_id"], timestamp, ACTION, request["key_id"], canonical_bytes(request).decode()))
            conn.execute("INSERT INTO autonomy(ts,actor,action,level,detail,approved_by,outcome) VALUES(?,?,?,?,?,?,?)",
                         (timestamp, "risk", ACTION, 4, request["reason"], f"key:{request['key_id']}", "ok"))
            conn.execute("INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                         (timestamp, "WARN", "signed_strategy_size_reduced", f"key:{request['key_id']}",
                          json.dumps({"request_id": request["request_id"], "strategy": strategy,
                                      "previous_multiplier": str(previous), "new_multiplier": str(proposed),
                                      "reason": request["reason"]})))
