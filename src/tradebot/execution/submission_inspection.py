"""Bounded read-only submission diagnostics, never recovery authorization."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from ..core.clock import iso, utcnow

_MAX_ROWS = 10000
_MAX_BYTES = 65536
_MAX_TOTAL_BYTES = 4 * 1024 * 1024


class SubmissionInspectionError(ValueError):
    pass


def inspect_pending_submissions(path: Path) -> dict[str, Any]:
    started = iso(utcnow())
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            query = """FROM kv p LEFT JOIN kv i ON i.key='order_identity:' || substr(p.key,20)
                       WHERE p.key GLOB 'submission_pending:*'"""
            limits = conn.execute("SELECT COUNT(*), COALESCE(SUM(length(CAST(p.value AS BLOB)) + "
                                  "COALESCE(length(CAST(i.value AS BLOB)),0)),0), "
                                  "COALESCE(MAX(MAX(length(CAST(p.value AS BLOB)), "
                                  "COALESCE(length(CAST(i.value AS BLOB)),0))),0) " + query).fetchone()
            if limits[0] > _MAX_ROWS or limits[1] > _MAX_TOTAL_BYTES or limits[2] > _MAX_BYTES:
                raise SubmissionInspectionError("submission snapshot exceeds size limit")
            rows = conn.execute("SELECT p.key,p.value,p.updated_at,i.value AS identity " + query +
                                " ORDER BY p.key LIMIT ?", (_MAX_ROWS + 1,)).fetchall()
            pending = []
            for row in rows:
                identifier = row['key'].removeprefix('submission_pending:')
                if len(identifier) != 64 or any(c not in '0123456789abcdef' for c in identifier):
                    raise SubmissionInspectionError("invalid submission identifier")
                try:
                    payload = json.loads(row['value'])
                    if (not isinstance(payload, dict) or not isinstance(payload.get('venue'), str)
                            or not isinstance(payload.get('client_id'), str)):
                        raise ValueError('invalid identity fields')
                    expected = hashlib.sha256(json.dumps([payload['venue'], payload['client_id']]).encode()).hexdigest()
                    if identifier != expected:
                        raise ValueError('identity digest mismatch')
                except (ValueError, TypeError) as exc:
                    raise SubmissionInspectionError("malformed submission record") from exc
                pending.append({'submission_id': identifier, 'updated_at': row['updated_at'],
                                'intent': payload, 'identity_record_matches': row['value'] == row['identity']})
            return {'read_started_at': started, 'read_finished_at': iso(utcnow()),
                    'pending_count': len(pending), 'pending': pending, 'verified': False,
                    'usable_for_reset': False, 'warning': 'Diagnostic only. A pending intent may have executed. '
                    'Do not resubmit or remove holds. Matching records or an empty list do not prove reconciliation.'}
        finally:
            conn.close()
    except SubmissionInspectionError:
        raise
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise SubmissionInspectionError('cannot inspect local submission state') from exc
