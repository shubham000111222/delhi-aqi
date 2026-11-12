"""Read-only, bounded views of persisted computed checks, not generated odds."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from ..core.clock import parse_iso
from ..core.store import Store


def order_ticket(check: dict[str, Any]) -> str:
    def number(data: dict[str, Any], key: str, *, percent: bool = False) -> str:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return 'unavailable'
        return f'{value * 100:.1f}%' if percent else f'{value:.3f}'

    sim = check.get('simulation')
    replay = check.get('replay')
    sim = sim if isinstance(sim, dict) else {}
    replay = replay if isinstance(replay, dict) else {}
    prefix = 'RESEARCH ONLY. ' if check.get('research_only') else ''
    return (f"{prefix}{check.get('trade_id', 'unknown candidate')}: "
            f"{check.get('verdict', 'unavailable')}; size multiplier {check.get('multiplier', 'unavailable')}. "
            f"Simulation: target first {number(sim, 'target_first', percent=True)}, "
            f"stop first {number(sim, 'stop_first', percent=True)}, "
            f"time exit {number(sim, 'time_exit', percent=True)}. "
            f"Net mean {number(sim, 'expected_net_r')} R, median {number(sim, 'median_r')} R, "
            f"5th percentile {number(sim, 'p05_r')} R; mean holding "
            f"{number(sim, 'expected_holding_minutes')} minutes. "
            f"Replay: net mean {number(replay, 'expected_net_r')} R; "
            f"insufficient evidence: {replay.get('insufficient', 'unavailable')}. "
            f"Reason: {check.get('reason', 'unavailable')}. "
            'OHLC first-touch order is modeled, not verified. A sanity check, not a profit promise.')


def check_history(store: Store, *, since: datetime | None = None,
                  until: datetime | None = None, limit: int = 200) -> dict[str, Any]:
    """Bounded newest-first audit page. Never imply the page is the complete ledger."""
    if not 1 <= limit <= 1000:
        raise ValueError('check history limit must be 1..1000')
    rows = store.events(kinds=['timemachine_check'], since=since, until=until, limit=limit + 1)
    checks = []
    for row in rows[:limit]:
        try:
            payload = row['payload']
            if not isinstance(payload, dict):
                raise ValueError('invalid check payload')
            parse_iso(row['ts'])
            checks.append({'ts': row['ts'], 'check': payload, 'ticket': order_ticket(payload)})
        except (ValueError, TypeError):
            checks.append({'ts': row['ts'], 'check': {'verdict': 'UNAVAILABLE'},
                           'ticket': 'Invalid persisted check; no computed odds available.'})
    return {'checks': checks, 'truncated': len(rows) > limit, 'limit': limit,
            'source': 'persisted event audit', 'filter_enabled': False}
