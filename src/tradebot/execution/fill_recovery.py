"""Read-only inspection of pending fill callbacks, never recovery authority.

A journal record can refer to a fill applied partly or completely before a crash.
Do not automatically replay or remove it. Local callback IDs are not venue trade IDs.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..core.clock import iso, utcnow

_MAX_ROWS = 10000
_MAX_BYTES = 65536
_MAX_TOTAL_BYTES = 4 * 1024 * 1024
_FIELDS = frozenset({"order_id", "symbol", "side", "price", "qty", "fee", "ts", "strategy", "liquidity",
                     "slippage_bps", "expected_price", "reason"})


class FillInspectionError(ValueError):
    """Local state could not be safely inspected."""


def inspect_pending_fills(path: Path) -> dict[str, Any]:
    """Return a bounded SQLite snapshot, preserving WAL, without modifying the database."""
    started = iso(utcnow())
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            limits = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(CAST(value AS BLOB))),0), "
                "COALESCE(MAX(length(CAST(value AS BLOB))),0) FROM kv WHERE key GLOB 'fill_pending:*'"
            ).fetchone()
            if limits[0] > _MAX_ROWS or limits[1] > _MAX_TOTAL_BYTES or limits[2] > _MAX_BYTES:
                raise FillInspectionError("pending-fill snapshot exceeds size limit")
            records = conn.execute(
                "SELECT key,value,updated_at FROM kv WHERE key GLOB 'fill_pending:*' ORDER BY key LIMIT ?",
                (_MAX_ROWS + 1,),
            ).fetchall()
            if len(records) > _MAX_ROWS:
                raise FillInspectionError("pending-fill row limit exceeded")
            pending: list[dict[str, Any]] = []
            for row in records:
                key, raw, updated_at = row["key"], row["value"], row["updated_at"]
                if not isinstance(key, str) or not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_BYTES:
                    raise FillInspectionError("invalid or oversized pending-fill record")
                identifier = key.removeprefix("fill_pending:")
                if len(identifier) != 32 or any(char not in "0123456789abcdef" for char in identifier):
                    raise FillInspectionError("invalid pending-fill identifier")
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError) as exc:
                    raise FillInspectionError("malformed pending-fill record") from exc
                if (not isinstance(payload, dict) or set(payload) != _FIELDS
                        or any(not isinstance(payload[k], str) for k in _FIELDS - {"expected_price"})
                        or payload["expected_price"] is not None and not isinstance(payload["expected_price"], str)
                        or not isinstance(updated_at, str)):
                    raise FillInspectionError("invalid pending-fill record shape")
                pending.append({"callback_id": identifier, "journal_updated_at": updated_at, "fill": payload})
            return {"read_started_at": started, "read_finished_at": iso(utcnow()), "pending_count": len(pending),
                    "pending": pending, "verified": False, "usable_for_reset": False,
                    "warning": "Diagnostic only. A callback may be partially applied; do not replay or clear it. "
                               "Local IDs do not identify venue trades, and absence does not prove reconciliation."}
        finally:
            conn.close()
    except FillInspectionError:
        raise
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise FillInspectionError("cannot read local state database") from exc
