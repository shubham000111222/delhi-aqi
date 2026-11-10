"""Offline Ed25519 approval for acknowledging a kill-incident tracking fault.

The signature authenticates possession of a provisioned key, not a human identity.
The public-key bundle and process/database access must be protected by OS controls.
This action never lowers the kill level or claims that positions are safe.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .config import Config
from .core.clock import Clock, iso, parse_iso, utcnow
from .core.store import Store
from .risk.live_guards import RampController

ACTION = "acknowledge_kill_incident_tracking_failure"
MAX_TTL = timedelta(minutes=15)
_FIELDS = frozenset({"version", "request_id", "action", "database_id", "mode", "key_id", "state_hash",
                     "reason", "issued_at", "expires_at"})


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def state_hash(state: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(state)).hexdigest()


def key_id(public_bytes: bytes) -> str:
    return hashlib.sha256(public_bytes).hexdigest()


class SignedFaultRecovery:
    """Action-bound recovery; the trusted key file is fixed relative to config root."""

    def __init__(self, config: Config, store: Store, clock: Clock | None = None) -> None:
        self.config = config
        self.store = store
        self.clock = clock
        self._mode = config.mode.value
        self._key_file = Path(config.settings.config_dir).resolve().parent / "operator-keys.json"

    def _now(self):
        return self.clock.now() if self.clock else utcnow()

    def _database_id(self) -> str:
        # Random database identity survives restart and prevents cross-database replay.
        with self.store.transaction() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='operator_database_id'").fetchone()
            if row is None:
                value = secrets.token_hex(32)
                conn.execute("INSERT INTO meta(key,value) VALUES('operator_database_id',?)", (value,))
                return value
            return str(row["value"])

    def _public_key(self, fingerprint: str) -> Ed25519PublicKey:
        raw_file = self._key_file.read_bytes()
        if len(raw_file) > 65536:
            raise ValueError("operator key bundle too large")
        bundle = json.loads(raw_file)
        if not isinstance(bundle, dict) or set(bundle) != {"version", "keys"} or bundle["version"] != 1:
            raise ValueError("invalid operator key bundle")
        keys = bundle["keys"]
        if not isinstance(keys, dict) or fingerprint not in keys:
            raise PermissionError("operator key is not trusted")
        try:
            raw = base64.b64decode(keys[fingerprint], validate=True)
            if len(raw) != 32 or key_id(raw) != fingerprint:
                raise ValueError("operator key fingerprint mismatch")
            return Ed25519PublicKey.from_public_bytes(raw)
        except (binascii.Error, TypeError, ValueError) as exc:
            raise ValueError("invalid operator public key") from exc

    def request(self, fingerprint: str, reason: str, *, ttl: timedelta = MAX_TTL) -> dict[str, Any]:
        self._public_key(fingerprint)
        if self.config.mode.value != self._mode:
            raise PermissionError("runtime mode changed")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValueError("a written recovery reason of at most 500 characters is required")
        if ttl <= timedelta(0) or ttl > MAX_TTL:
            raise ValueError("recovery approval TTL must be between zero and 15 minutes")
        state = self.store.kill_state()
        if state.get("level") not in ("L1", "L2", "L3") or state.get("incident_tracking_failed") is not True:
            raise ValueError("no stopped kill state with an incident-tracking fault")
        if self.store.has_pending_kill_incident():
            raise ValueError("pending kill incident must be recovered first")
        now = self._now()
        return {"version": 1, "request_id": secrets.token_hex(32), "action": ACTION,
                "database_id": self._database_id(), "mode": self._mode, "key_id": fingerprint,
                "state_hash": state_hash(state), "reason": reason.strip(),
                "issued_at": iso(now), "expires_at": iso(now + ttl)}

    def apply(self, request: dict[str, Any], signature_b64: str) -> None:
        if not isinstance(request, dict) or set(request) != _FIELDS or type(request.get("version")) is not int or request.get("version") != 1:
            raise ValueError("invalid signed recovery request")
        if any(not isinstance(request.get(k), str) for k in _FIELDS - {"version"}):
            raise ValueError("invalid signed recovery fields")
        if request["action"] != ACTION or request["mode"] != self._mode or self.config.mode.value != self._mode:
            raise PermissionError("signed recovery action or mode mismatch")
        if (len(request["request_id"]) != 64 or len(request["state_hash"]) != 64
                or not request["reason"].strip() or len(request["reason"]) > 500):
            raise ValueError("invalid signed recovery values")
        now = self._now()
        try:
            issued, expires = parse_iso(request["issued_at"]), parse_iso(request["expires_at"])
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("invalid signed recovery timestamps") from exc
        if issued > now or now >= expires or expires <= issued or expires - issued > MAX_TTL:
            raise PermissionError("signed recovery request is expired or not yet valid")
        key = self._public_key(request["key_id"])
        try:
            signature = base64.b64decode(signature_b64, validate=True)
            key.verify(signature, canonical_bytes(request))
        except (InvalidSignature, binascii.Error, TypeError, ValueError) as exc:
            raise PermissionError("invalid operator signature") from exc
        with self.store.transaction() as conn:
            # Re-check expiry and trust after acquiring the write lock; verification
            # before waiting for a busy database must not extend an approval's TTL.
            now = self._now()
            if issued > now or now >= expires:
                raise PermissionError("signed recovery expired while waiting for database")
            if self.config.mode.value != self._mode:
                raise PermissionError("runtime mode changed")
            try:
                self._public_key(request["key_id"]).verify(signature, canonical_bytes(request))
            except InvalidSignature as exc:
                raise PermissionError("invalid operator signature") from exc
            timestamp = iso(now)
            db_row = conn.execute("SELECT value FROM meta WHERE key='operator_database_id'").fetchone()
            if db_row is None or db_row["value"] != request["database_id"]:
                raise PermissionError("signed recovery database mismatch")
            if conn.execute("SELECT 1 FROM signed_action_consumptions WHERE request_id=?",
                            (request["request_id"],)).fetchone() is not None:
                raise PermissionError("signed recovery request already consumed")
            row = conn.execute("SELECT value FROM kv WHERE key='kill_state'").fetchone()
            state = json.loads(row["value"]) if row else None
            if (not isinstance(state, dict) or state_hash(state) != request["state_hash"]
                    or state.get("level") not in ("L1", "L2", "L3")
                    or state.get("incident_tracking_failed") is not True):
                raise PermissionError("kill state changed; signed recovery is stale")
            if conn.execute("SELECT 1 FROM kv WHERE key GLOB 'kill_incident_pending:*' LIMIT 1").fetchone():
                raise PermissionError("pending kill incident must be recovered first")
            if conn.execute("SELECT 1 FROM kv WHERE key='ramp_incident_tracking_invalid'").fetchone():
                raise PermissionError("ramp incident tracking requires separate investigation")
            RampController._state_in_transaction(conn)
            # The failed outbox may have preceded other escalations. The current state
            # alone cannot identify which incident was lost, so never fabricate one.
            # Keep a durable independent hold until a separate reconciliation workflow
            # exists; this signed action acknowledges the fault, not incident recovery.
            conn.execute(
                "INSERT INTO kv(key,value,updated_at) VALUES('ramp_incident_tracking_invalid',?,?)",
                (json.dumps({"request_id": request["request_id"], "reason": "incident history unverified"}),
                 timestamp),
            )
            transition = conn.execute(
                "INSERT INTO kill_events(ts,level,previous,trigger,reason,actor,cooldown_until) VALUES(?,?,?,?,?,?,?)",
                (timestamp, state["level"], state["level"], "tracking_fault_ack", request["reason"],
                 f"key:{request['key_id']}", state.get("cooldown_until")),
            )
            state["generation"] = int(transition.lastrowid or 0)
            state["incident_tracking_failed"] = False
            conn.execute("UPDATE kv SET value=?,updated_at=? WHERE key='kill_state'",
                         (json.dumps(state), timestamp))
            conn.execute("INSERT INTO signed_action_consumptions(request_id,ts,action,key_id,payload) VALUES(?,?,?,?,?)",
                         (request["request_id"], timestamp, ACTION, request["key_id"], canonical_bytes(request).decode()))
            conn.execute("INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                         (timestamp, "CRIT", "signed_tracking_fault_ack", f"key:{request['key_id']}", json.dumps({
                             "request_id": request["request_id"], "incident_history": "unverified",
                             "level": state["level"], "reason": request["reason"],
                         })))
