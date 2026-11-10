"""SQLite persistence: state + an append-only event log.

WAL mode, one file per mode (paper / testnet / live). Everything the bot needs to
survive a restart lives here: kill-switch level, open orders, positions, equity,
day-start equity, peak equity, loss streaks, ramp stage and the audit chain.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .clock import iso, parse_iso, utc, utcnow
from .models import (
    EquitySnapshot,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Position,
    RiskVerdict,
    Side,
    Signal,
    TimeInForce,
)
from .money import ZERO, D, money_json, to_str

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'bot',
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts);

CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    stop TEXT,
    target TEXT,
    confidence TEXT,
    reason TEXT,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    approved INTEGER NOT NULL,
    reason TEXT,
    failed_check TEXT,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    exchange_order_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    qty TEXT NOT NULL,
    price TEXT,
    filled_qty TEXT NOT NULL DEFAULT '0',
    avg_fill_price TEXT NOT NULL DEFAULT '0',
    fee TEXT NOT NULL DEFAULT '0',
    strategy TEXT,
    signal_id TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, updated_at);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    qty TEXT NOT NULL,
    fee TEXT NOT NULL,
    liquidity TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_fills_symbol ON fills(symbol, ts);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    qty TEXT NOT NULL,
    avg_entry TEXT NOT NULL,
    exit_price TEXT,
    exit_reason TEXT,
    realized_pnl TEXT NOT NULL DEFAULT '0',
    fees TEXT NOT NULL DEFAULT '0',
    payload TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_positions_open
    ON positions(symbol, strategy) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    equity TEXT NOT NULL,
    cash TEXT NOT NULL,
    positions_value TEXT NOT NULL,
    realized TEXT NOT NULL,
    unrealized TEXT NOT NULL,
    fees TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kill_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    previous TEXT,
    trigger TEXT,
    reason TEXT,
    actor TEXT,
    cooldown_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_kill_ts ON kill_events(ts);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    trigger TEXT,
    summary TEXT,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT,
    strategy TEXT,
    params TEXT,
    data_hash TEXT,
    git_commit TEXT,
    split TEXT,
    seed INTEGER,
    metrics TEXT,
    artifacts TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS autonomy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    level INTEGER NOT NULL,
    detail TEXT,
    approved_by TEXT,
    outcome TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS strategy_fault_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_fault_attempts ON strategy_fault_attempts(strategy,ts);

CREATE TABLE IF NOT EXISTS disabled_strategies (
    strategy TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    numbers TEXT NOT NULL,
    state TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT
);

CREATE TABLE IF NOT EXISTS signed_action_consumptions (
    request_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    key_id TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holdout_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    strategy TEXT NOT NULL,
    run_id TEXT,
    override INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);
"""


class Store:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    # --- plumbing ---------------------------------------------------------
    def _configure(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),)
                )
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {row['value']} is newer than this code ({SCHEMA_VERSION}); "
                    "roll back the image instead of downgrading the database"
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    # --- events (append-only) --------------------------------------------
    def log_event(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        level: str = "INFO",
        actor: str = "bot",
        ts: datetime | None = None,
    ) -> int:
        cur = self._exec(
            "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
            (iso(ts or utcnow()), level, kind, actor, json.dumps(payload or {}, default=str)),
        )
        return int(cur.lastrowid or 0)

    def events(
        self,
        limit: int = 200,
        kinds: Iterable[str] | None = None,
        since: datetime | None = None,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if kinds:
            kinds = list(kinds)
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        if since:
            sql += " AND ts >= ?"
            params.append(iso(since))
        if level:
            sql += " AND level = ?"
            params.append(level)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._query(sql, params)
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "level": r["level"],
                "kind": r["kind"],
                "actor": r["actor"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    @staticmethod
    def _disable_strategy_tx(conn: sqlite3.Connection, name: str, reason: str,
                             faults: int, timestamp: str) -> bool:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO disabled_strategies(strategy,ts,reason) VALUES(?,?,?)",
            (name, timestamp, reason),
        )
        if not cursor.rowcount:
            return False
        conn.execute(
            "INSERT INTO autonomy(ts,actor,action,level,detail,outcome) VALUES(?,?,?,?,?,?)",
            (timestamp, "supervisor", "disable_strategy", 3, reason, "ok"),
        )
        conn.execute(
            "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
            (timestamp, "CRIT", "strategy_disabled", "supervisor", json.dumps({
                "strategy": name, "reason": reason, "faults_last_hour": faults,
            })),
        )
        return True

    def disable_strategy(self, name: str, reason: str, *, faults: int, ts: datetime) -> bool:
        """Latch a strategy disabled, with its autonomy record and event in one transaction."""
        if not isinstance(name, str) or not name:
            raise ValueError("strategy name is required")
        with self.transaction() as conn:
            return self._disable_strategy_tx(conn, name, reason, faults, iso(ts))

    @staticmethod
    def _fault_timestamp(ts: datetime) -> str:
        return utc(ts).isoformat(timespec="microseconds")

    def strategy_fault_attempts(self, name: str, *, now: datetime) -> list[datetime]:
        cutoff = self._fault_timestamp(now - timedelta(hours=1))
        rows = self._query("SELECT ts FROM strategy_fault_attempts WHERE strategy=? AND ts>=? ORDER BY ts",
                           (name, cutoff))
        return [parse_iso(row["ts"]) for row in rows]

    def strategy_fault_paused(self, name: str, *, now: datetime) -> bool:
        cutoff = self._fault_timestamp(now - timedelta(hours=1))
        return bool(self._query("SELECT 1 FROM strategy_fault_attempts WHERE strategy=? AND ts>=? LIMIT 1",
                                (name, cutoff)))

    def record_strategy_fault(self, name: str, reason: str, *, now: datetime) -> bool:
        """Atomically pause the first three faults and latch disabled on the fourth.

        Future-dated attempts also count, so a backward clock cannot erase the budget.
        Returns whether the strategy is disabled. No automatic deletion/re-enable occurs.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("strategy name is required")
        timestamp = self._fault_timestamp(now)
        cutoff = self._fault_timestamp(now - timedelta(hours=1))
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM disabled_strategies WHERE strategy=?", (name,)).fetchone():
                return True
            count = conn.execute("SELECT COUNT(*) FROM strategy_fault_attempts WHERE strategy=? AND ts>=?",
                                 (name, cutoff)).fetchone()[0]
            if count >= 3:
                self._disable_strategy_tx(conn, name, reason, count, timestamp)
                return True
            conn.execute("INSERT INTO strategy_fault_attempts(strategy,ts) VALUES(?,?)", (name, timestamp))
            conn.execute("INSERT INTO autonomy(ts,actor,action,level,detail,outcome) VALUES(?,?,?,?,?,?)",
                         (timestamp, "supervisor", "pause_strategy", 3, reason, "ok"))
            conn.execute("INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                         (timestamp, "WARN", "strategy_paused", "supervisor", json.dumps({
                             "strategy": name, "hours": 1, "reason": reason, "faults_last_hour": count + 1,
                         })))
        return False

    def disabled_strategies(self) -> dict[str, str]:
        return {row["strategy"]: row["reason"] for row in
                self._query("SELECT strategy,reason FROM disabled_strategies")}

    def strategy_disabled(self, name: str) -> bool:
        return bool(self._query("SELECT 1 FROM disabled_strategies WHERE strategy=?", (name,)))

    @staticmethod
    def _strategy_size_value(raw: Any) -> Decimal:
        if not isinstance(raw, str) or len(raw) > 64:
            raise ValueError("invalid strategy size multiplier")
        try:
            value = Decimal(raw)
            if not value.is_finite() or value <= 0 or value > 1:
                raise ValueError("invalid strategy size multiplier")
            return value
        except ArithmeticError as exc:
            raise ValueError("invalid strategy size multiplier") from exc

    def strategy_size_multiplier(self, name: str) -> Decimal:
        rows = self._query("SELECT value FROM kv WHERE key=?", (f"size_mult_{name}",))
        return self._strategy_size_value(json.loads(rows[0]["value"])) if rows else Decimal("1")

    def reduce_strategy_size(self, name: str, multiplier: Decimal, *, now: datetime, reason: str) -> bool:
        """Persist only a stricter size multiplier, atomically with its audit."""
        if not isinstance(name, str) or not name or not isinstance(multiplier, Decimal):
            raise ValueError("invalid strategy size reduction")
        value = self._strategy_size_value(str(multiplier))
        key = f"size_mult_{name}"
        timestamp = iso(now)
        with self.transaction() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            previous = self._strategy_size_value(json.loads(row["value"])) if row else Decimal("1")
            if value >= previous:
                return False
            conn.execute("INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                         (key, json.dumps(str(value)), timestamp))
            conn.execute("INSERT INTO autonomy(ts,actor,action,level,detail,outcome) VALUES(?,?,?,?,?,?)",
                         (timestamp, "risk", "reduce_strategy_size", 3, reason, "ok"))
            conn.execute("INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                         (timestamp, "WARN", "size_reduction", "risk", json.dumps({
                             "strategy": name, "from": str(previous), "multiplier": str(value), "reason": reason,
                         })))
        return True

    def record_strategy_trade_risk(
        self, name: str, *, loss: bool, closed_at: datetime, now: datetime,
        halve_at: int, pause_at: int, pause_hours: int,
    ) -> int:
        """Serialize streak changes and risk controls across processes.

        Older/equal-time wins do not reset a newer streak; late losses are counted
        conservatively. This does not deduplicate trade callbacks or atomically close
        the ledger position, so it must not be treated as verified trade history.
        """
        if (not isinstance(name, str) or not name or type(loss) is not bool
                or type(halve_at) is not int or type(pause_at) is not int or type(pause_hours) is not int
                or halve_at < 1 or pause_at < halve_at or pause_hours < 1):
            raise ValueError("invalid strategy loss policy")
        timestamp = iso(now)
        closed_at = utc(closed_at)
        with self.transaction() as conn:
            def read(key: str, default: Any) -> Any:
                row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
                return json.loads(row["value"]) if row else default

            def write(key: str, value: Any) -> None:
                conn.execute("INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                             (key, json.dumps(value), timestamp))

            streaks = read("consecutive_losses", {})
            latest = read("consecutive_losses_latest", {})
            if (not isinstance(streaks, dict) or not isinstance(latest, dict)
                    or any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in streaks.items())
                    or any(not isinstance(k, str) or not isinstance(v, str) for k, v in latest.items())):
                raise ValueError("invalid persisted loss streak")
            last = parse_iso(latest[name]) if name in latest else None
            streak = streaks.get(name, 0)
            if loss:
                streak += 1
            elif last is None or closed_at > last:
                streak = 0
            if last is None or closed_at > last:
                latest[name] = iso(closed_at)
            streaks[name] = streak
            write("consecutive_losses", streaks)
            write("consecutive_losses_latest", latest)
            if loss and streak >= halve_at:
                key = f"size_mult_{name}"
                old = self._strategy_size_value(read(key, "1"))
                if old > Decimal("0.5"):
                    write(key, "0.5")
                    reason = f"{streak} consecutive losses"
                    conn.execute("INSERT INTO autonomy(ts,actor,action,level,detail,outcome) VALUES(?,?,?,?,?,?)",
                                 (timestamp, "risk", "reduce_strategy_size", 3, reason, "ok"))
                    conn.execute("INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                                 (timestamp, "WARN", "size_reduction", "risk", json.dumps({
                                     "strategy": name, "from": str(old), "multiplier": "0.5", "reason": reason,
                                 })))
            if loss and streak >= pause_at:
                paused = read("paused_strategies", {})
                if not isinstance(paused, dict):
                    raise ValueError("invalid persisted strategy pauses")
                until = utc(now) + timedelta(hours=pause_hours)
                existing = paused.get(name)
                should_extend = name not in paused
                if name in paused and isinstance(existing, dict) and isinstance(existing.get("until"), str):
                    # Malformed/indefinite holds remain in force.
                    with suppress(ValueError, OverflowError, TypeError):
                        should_extend = parse_iso(existing["until"]) < until
                if should_extend:
                    reason = f"{streak} consecutive losses"
                    paused[name] = {"until": iso(until), "reason": reason}
                    write("paused_strategies", paused)
                    conn.execute("INSERT INTO autonomy(ts,actor,action,level,detail,outcome) VALUES(?,?,?,?,?,?)",
                                 (timestamp, "risk", "pause_strategy", 3, reason, "ok"))
                    conn.execute("INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                                 (timestamp, "WARN", "strategy_paused", "risk", json.dumps({
                                     "strategy": name, "until": iso(until), "reason": reason,
                                 })))
        return streak

    def strategy_paused(self, name: str, *, now: datetime) -> bool:
        """All durable strategy holds, including malformed legacy pause records."""
        if self.strategy_disabled(name) or self.strategy_fault_paused(name, now=now):
            return True
        try:
            streaks = self.kv_get("consecutive_losses", {})
            latest = self.kv_get("consecutive_losses_latest", {})
            if (not isinstance(streaks, dict) or not isinstance(latest, dict)
                    or any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in streaks.items())
                    or any(not isinstance(k, str) or not isinstance(v, str) for k, v in latest.items())):
                return True
            for value in latest.values():
                parse_iso(value)
        except (ValueError, TypeError, OverflowError, AttributeError):
            return True
        rows = self._query("SELECT value FROM kv WHERE key='paused_strategies'")
        if not rows:
            return False
        try:
            paused = json.loads(rows[0]["value"])
            if not isinstance(paused, dict):
                return True
            if name not in paused:
                return False
            info = paused[name]
            if not isinstance(info, dict) or not isinstance(info.get("until"), str):
                return True
            return utc(now) < parse_iso(info["until"])
        except (TypeError, ValueError, OverflowError, AttributeError):
            return True

    def extend_strategy_pause(self, name: str, *, until: datetime, reason: str, now: datetime) -> bool:
        """Atomically extend a timed pause and record the action; never shorten a hold."""
        if not isinstance(name, str) or not name or not isinstance(reason, str):
            raise ValueError("invalid strategy pause")
        if utc(until) <= utc(now):
            raise ValueError("pause deadline must be in the future")
        timestamp = iso(now)
        with self.transaction() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key='paused_strategies'").fetchone()
            paused = json.loads(row["value"]) if row else {}
            if not isinstance(paused, dict):
                raise ValueError("invalid persisted strategy pauses")
            if name in paused:
                try:
                    info = paused[name]
                    if not isinstance(info, dict) or not isinstance(info.get("until"), str):
                        return False  # an indefinite or corrupt hold cannot be weakened
                    if parse_iso(info["until"]) >= utc(until):
                        return False
                except (ValueError, TypeError, OverflowError, AttributeError):
                    return False
            paused[name] = {"until": iso(until), "reason": reason}
            conn.execute("INSERT INTO kv(key,value,updated_at) VALUES('paused_strategies',?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                         (json.dumps(paused), timestamp))
            conn.execute("INSERT INTO autonomy(ts,actor,action,level,detail,outcome) VALUES(?,?,?,?,?,?)",
                         (timestamp, "risk", "pause_strategy", 3, reason, "ok"))
            conn.execute("INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                         (timestamp, "WARN", "strategy_paused", "risk", json.dumps({
                             "strategy": name, "until": iso(until), "reason": reason,
                         })))
        return True

    # --- autonomy ledger and approvals (§21.7) ----------------------------
    def add_autonomy(
        self,
        *,
        ts: str,
        actor: str,
        action: str,
        level: int,
        detail: str = "",
        approved_by: str | None = None,
        outcome: str = "ok",
        payload: dict[str, Any],
    ) -> None:
        """Commit the ledger row and its event together, or commit neither."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO autonomy (ts, actor, action, level, detail, approved_by, outcome)"
                " VALUES (?,?,?,?,?,?,?)",
                (ts, actor, action, int(level), detail, approved_by, outcome),
            )
            conn.execute(
                "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                (ts, "WARN" if outcome != "ok" else "INFO", "autonomy", actor,
                 json.dumps(payload, default=str)),
            )

    def autonomy_actions(self, limit: int = 100, actor: str | None = None) -> list[dict[str, Any]]:
        """Newest last, so a printed transcript reads in the order things happened."""
        sql = "SELECT * FROM autonomy"
        params: list[Any] = []
        if actor:
            sql += " WHERE actor=?"
            params.append(actor)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self._query(sql, params)
        labels = {0: "observe", 1: "alert", 2: "correct", 3: "protect", 4: "allocate", 5: "self-modify"}
        return [
            {
                **dict(row),
                "level_label": labels.get(int(row["level"]), str(row["level"])),
            }
            for row in reversed(rows)
        ]

    @staticmethod
    def _approval_timestamp(value: datetime) -> str:
        # Fixed width is essential: lexical SQLite comparisons of ISO strings with
        # and without fractional seconds do not sort chronologically.
        return utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def save_approval(self, record: dict[str, Any]) -> None:
        """Create a request and its audit event atomically; neither authorizes execution."""
        created = self._approval_timestamp(parse_iso(record["created_at"]))
        expires = self._approval_timestamp(parse_iso(record["expires_at"]))
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO approvals (id, ts, actor, action, detail, numbers, state, expires_at,"
                " decided_at, decided_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (record["id"], created, record["actor"], record["action"], record.get("detail", ""),
                 json.dumps(record.get("numbers") or {}, sort_keys=True, allow_nan=False),
                 record["state"], expires, record.get("decided_at"), record.get("decided_by")),
            )
            conn.execute(
                "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                (created, "INFO", "approval_requested", record["actor"], json.dumps({
                    "id": record["id"], "action": record["action"], "numbers": record["numbers"],
                    "expires_at": expires, "authentication": "not_verified",
                }, sort_keys=True, allow_nan=False)),
            )

    def transition_approval(
        self, request_id: str, *, state: str, now: datetime, by: str | None = None,
        detail: str | None = None,
    ) -> bool:
        """Atomically decide once and record it. Identity is not authenticated."""
        if state not in {"approved", "rejected", "expired"}:
            raise ValueError("invalid approval transition")
        by = by.strip() if by else None
        if state != "expired" and not by:
            raise ValueError("decision actor is required")
        comparison = "<=" if state == "expired" else ">"
        now_str = self._approval_timestamp(now)
        with self.transaction() as conn:
            row = conn.execute("SELECT actor,action FROM approvals WHERE id=?", (request_id,)).fetchone()
            if row is None or (state == "approved" and row["actor"] == by):
                return False
            cursor = conn.execute(
                "UPDATE approvals SET state=?, decided_at=?, decided_by=?, detail=COALESCE(?, detail) "
                f"WHERE id=? AND state='pending' AND ts<=? AND expires_at {comparison} ?",
                (state, now_str, by, detail, request_id, now_str, now_str),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                (now_str, "WARN", f"approval_{state}", by or "autonomy", json.dumps({
                    "id": request_id, "action": row["action"], "requester": row["actor"],
                    "decided_by": by, "authentication": "not_verified",
                })),
            )
            return True

    def approval(self, request_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM approvals WHERE id=?", (request_id,))
        return self._approval_row(rows[0]) if rows else None

    def approvals(self, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM approvals"
        params: list[Any] = []
        if state:
            sql += " WHERE state=?"
            params.append(state)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        return [self._approval_row(row) for row in self._query(sql, params)]

    @staticmethod
    def _approval_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "actor": row["actor"],
            "action": row["action"],
            "detail": row["detail"] or "",
            "numbers": json.loads(row["numbers"] or "{}"),
            "state": row["state"],
            "created_at": parse_iso(row["ts"]),
            "expires_at": parse_iso(row["expires_at"]),
            "decided_at": parse_iso(row["decided_at"]) if row["decided_at"] else None,
            "decided_by": row["decided_by"],
        }

    def count_events(self, kind: str, since: datetime | None = None) -> int:
        if since:
            row = self._query(
                "SELECT COUNT(*) AS c FROM events WHERE kind=? AND ts>=?", (kind, iso(since))
            )[0]
        else:
            row = self._query("SELECT COUNT(*) AS c FROM events WHERE kind=?", (kind,))[0]
        return int(row["c"])

    # --- signals and risk verdicts ---------------------------------------
    def save_signal(self, signal: Signal) -> None:
        self._exec(
            """INSERT OR REPLACE INTO signals
               (id,ts,strategy,symbol,side,timeframe,stop,target,confidence,reason,payload)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal.id,
                iso(signal.ts),
                signal.strategy,
                signal.symbol,
                signal.side.value,
                signal.timeframe,
                money_json(signal.stop),
                money_json(signal.target),
                to_str(signal.confidence, 4),
                signal.reason,
                json.dumps(signal.to_dict(), default=str),
            ),
        )

    def save_verdict(self, signal_id: str, verdict: RiskVerdict, ts: datetime | None = None) -> None:
        failed = verdict.failed[0].name if verdict.failed else None
        self._exec(
            """INSERT INTO risk_verdicts(ts,signal_id,approved,reason,failed_check,payload)
               VALUES(?,?,?,?,?,?)""",
            (
                iso(ts or utcnow()),
                signal_id,
                int(verdict.approved),
                verdict.reason,
                failed,
                json.dumps(verdict.to_dict(), default=str),
            ),
        )

    def signals(self, limit: int = 100, since: datetime | None = None) -> list[dict[str, Any]]:
        if since:
            rows = self._query(
                "SELECT * FROM signals WHERE ts>=? ORDER BY ts DESC LIMIT ?", (iso(since), limit)
            )
        else:
            rows = self._query("SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,))
        return [json.loads(r["payload"]) for r in rows]

    def verdicts(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM risk_verdicts ORDER BY id DESC LIMIT ?", (limit,))
        return [
            {
                "ts": r["ts"],
                "signal_id": r["signal_id"],
                "approved": bool(r["approved"]),
                "reason": r["reason"],
                "failed_check": r["failed_check"],
                "checks": json.loads(r["payload"]).get("checks", []),
            }
            for r in rows
        ]

    # --- orders / fills ---------------------------------------------------
    def upsert_order(self, order: Order) -> None:
        now = iso(utcnow())
        self._exec(
            """INSERT INTO orders(client_order_id,exchange_order_id,created_at,updated_at,symbol,side,
                    type,status,qty,price,filled_qty,avg_fill_price,fee,strategy,signal_id,payload)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(client_order_id) DO UPDATE SET
                    exchange_order_id=excluded.exchange_order_id,
                    updated_at=excluded.updated_at,
                    status=excluded.status,
                    filled_qty=excluded.filled_qty,
                    avg_fill_price=excluded.avg_fill_price,
                    fee=excluded.fee,
                    payload=excluded.payload""",
            (
                order.client_order_id,
                order.exchange_order_id,
                iso(order.created_at or utcnow()),
                now,
                order.symbol,
                order.side.value,
                order.type.value,
                order.status.value,
                to_str(order.qty),
                money_json(order.price),
                to_str(order.filled_qty),
                to_str(order.avg_fill_price),
                to_str(order.fee),
                order.strategy,
                order.signal_id,
                json.dumps(order.to_dict(), default=str),
            ),
        )

    def add_fill(self, fill: Fill) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO fills(ts,order_id,symbol,side,price,qty,fee,liquidity,payload)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    iso(fill.ts),
                    fill.order_id,
                    fill.symbol,
                    fill.side.value,
                    to_str(fill.price),
                    to_str(fill.qty),
                    to_str(fill.fee),
                    fill.liquidity,
                    json.dumps(fill.to_dict(), default=str),
                ),
            )
            conn.execute(
                "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                (iso(fill.ts), "INFO", "fill", "execution", json.dumps(fill.to_dict(), default=str)),
            )

    def fills(
        self,
        limit: int = 500,
        symbol: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM fills WHERE 1=1"
        params: list[Any] = []
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        if since:
            sql += " AND ts>=?"
            params.append(iso(since))
        if until:
            sql += " AND ts<?"
            params.append(iso(until))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [json.loads(r["payload"]) for r in self._query(sql, params)]

    def open_orders(self) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM orders WHERE status IN ('NEW','SUBMITTED','PARTIALLY_FILLED') ORDER BY created_at"
        )
        return [json.loads(r["payload"]) for r in rows]

    def orders(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,))
        return [json.loads(r["payload"]) for r in rows]

    def order_status_counts(self, since: datetime) -> dict[str, int]:
        rows = self._query(
            "SELECT status, COUNT(*) AS c FROM orders WHERE created_at>=? GROUP BY status", (iso(since),)
        )
        return {r["status"]: int(r["c"]) for r in rows}

    # --- positions --------------------------------------------------------
    def save_position(self, pos: Position) -> int:
        payload = json.dumps(_position_payload(pos), default=str)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id,payload FROM positions WHERE symbol=? AND strategy=? AND closed_at IS NULL",
                (pos.symbol, pos.strategy),
            ).fetchone()
            if row:
                # A later fill or stale writer must not overwrite a tightened stop. Keep
                # accounting updates, but restore any persisted stronger protection.
                try:
                    prior = json.loads(row["payload"])
                    old_base = D(prior["stop"])
                    old_trailing = D(prior["trailing_stop"]) if prior.get("trailing_stop") is not None else None
                    if (not old_base.is_finite() or old_base <= 0 or not pos.stop.is_finite()
                            or pos.stop <= 0 or (old_trailing is not None
                            and (not old_trailing.is_finite() or old_trailing <= 0))
                            or (pos.trailing_stop is not None
                            and (not pos.trailing_stop.is_finite() or pos.trailing_stop <= 0))):
                        raise ValueError("invalid stop")
                except (ArithmeticError, TypeError, ValueError, KeyError, AttributeError) as exc:
                    raise ValueError("cannot update position with invalid stop") from exc
                widened = pos.stop < old_base or (old_trailing is not None and (
                    pos.trailing_stop is None or pos.trailing_stop < old_trailing
                ))
                if widened:
                    pos.stop = max(pos.stop, old_base)
                    if old_trailing is not None:
                        pos.trailing_stop = max(pos.trailing_stop or old_trailing, old_trailing)
                    payload = json.dumps(_position_payload(pos), default=str)
                    conn.execute(
                        "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                        (iso(utcnow()), "WARN", "autonomy_refused", "portfolio", json.dumps({
                            "action": "widen_stop", "symbol": pos.symbol, "strategy": pos.strategy,
                            "reason": "persisted stop ratchet cannot be widened",
                        })),
                    )
                conn.execute(
                    "UPDATE positions SET qty=?, avg_entry=?, realized_pnl=?, fees=?, payload=? WHERE id=?",
                    (to_str(pos.qty), to_str(pos.avg_entry), to_str(pos.realized_pnl), to_str(pos.fees), payload, row["id"]),
                )
                return int(row["id"])
            cur = conn.execute(
                """INSERT INTO positions(symbol,strategy,opened_at,closed_at,qty,avg_entry,realized_pnl,fees,payload)
                   VALUES(?,?,?,NULL,?,?,?,?,?)""",
                (
                    pos.symbol,
                    pos.strategy,
                    iso(pos.opened_at),
                    to_str(pos.qty),
                    to_str(pos.avg_entry),
                    to_str(pos.realized_pnl),
                    to_str(pos.fees),
                    payload,
                ),
            )
            return int(cur.lastrowid or 0)

    def tighten_trailing_stop(
        self, symbol: str, strategy: str, candidate: Decimal, *, ts: datetime | None = None,
    ) -> Decimal:
        """Atomically ratchet an open long's software stop and record the change.

        Concurrent callers always receive the greatest committed effective stop. This
        protects this mutation path, not arbitrary SQL or venue-side protective orders.
        """
        if not isinstance(candidate, Decimal) or not candidate.is_finite() or candidate <= 0:
            raise ValueError("trailing stop must be a finite positive Decimal")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id,payload FROM positions WHERE symbol=? AND strategy=? AND closed_at IS NULL",
                (symbol, strategy),
            ).fetchone()
            if row is None:
                raise ValueError("cannot tighten stop without a persisted open position")
            try:
                payload = json.loads(row["payload"])
                base = D(payload["stop"])
                trailing = D(payload["trailing_stop"]) if payload.get("trailing_stop") is not None else base
                if not base.is_finite() or not trailing.is_finite() or base <= 0 or trailing <= 0:
                    raise ValueError("invalid persisted stop")
            except (ArithmeticError, TypeError, ValueError, KeyError, AttributeError) as exc:
                raise ValueError("cannot tighten invalid persisted stop") from exc
            current = max(base, trailing)
            if candidate <= current:
                return current
            payload["trailing_stop"] = to_str(candidate)
            conn.execute("UPDATE positions SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
            conn.execute(
                "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                (iso(ts or utcnow()), "INFO", "trailing_stop_tightened", "portfolio", json.dumps({
                    "symbol": symbol, "strategy": strategy, "previous": to_str(current),
                    "stop": to_str(candidate),
                })),
            )
            return candidate

    def close_position(
        self, pos: Position, exit_price: Decimal, exit_reason: str, closed_at: datetime | None = None
    ) -> None:
        """Persist the closing side of a position with everything reports need.

        The payload is denormalised on purpose: a closed trade must be readable
        without joining back to the position and without recomputing R multiples.
        """
        risk = pos.avg_entry - pos.initial_stop
        r_multiple = ((exit_price - pos.avg_entry) / risk) if risk > 0 else ZERO
        payload = json.dumps(
            _position_payload(pos)
            | {
                "exit_price": to_str(exit_price, 8),
                "exit": to_str(exit_price, 8),
                "entry": to_str(pos.avg_entry, 8),
                "exit_reason": exit_reason,
                "closed_at": iso(closed_at or utcnow()),
                "pnl": to_str(pos.realized_pnl, 8),
                "r_multiple": to_str(r_multiple, 4),
            },
            default=str,
        )
        self._exec(
            """UPDATE positions SET closed_at=?, exit_price=?, exit_reason=?, qty=?, realized_pnl=?,
                   fees=?, payload=?
               WHERE symbol=? AND strategy=? AND closed_at IS NULL""",
            (
                iso(closed_at or utcnow()),
                to_str(exit_price),
                exit_reason,
                to_str(pos.qty),
                to_str(pos.realized_pnl),
                to_str(pos.fees),
                payload,
                pos.symbol,
                pos.strategy,
            ),
        )

    def open_positions(self) -> list[Position]:
        rows = self._query("SELECT * FROM positions WHERE closed_at IS NULL ORDER BY opened_at")
        return [_position_from_row(r) for r in rows]

    def closed_positions(self, since: datetime | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        if since:
            rows = self._query(
                "SELECT * FROM positions WHERE closed_at IS NOT NULL AND closed_at>=? ORDER BY closed_at DESC LIMIT ?",
                (iso(since), limit),
            )
        else:
            rows = self._query(
                "SELECT * FROM positions WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT ?", (limit,)
            )
        out = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload.setdefault("closed_at", row["closed_at"])
            payload.setdefault("exit_price", row["exit_price"])
            payload.setdefault("exit_reason", row["exit_reason"])
            payload.setdefault("pnl", payload.get("realized_pnl", "0"))
            out.append(payload)
        return out

    # --- equity -----------------------------------------------------------
    def add_equity(self, snap: EquitySnapshot) -> None:
        self._exec(
            "INSERT INTO equity(ts,equity,cash,positions_value,realized,unrealized,fees) VALUES(?,?,?,?,?,?,?)",
            (
                iso(snap.ts),
                to_str(snap.equity),
                to_str(snap.cash),
                to_str(snap.positions_value),
                to_str(snap.realized_pnl),
                to_str(snap.unrealized_pnl),
                to_str(snap.fees),
            ),
        )

    def equity_series(self, since: datetime | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        if since:
            rows = self._query("SELECT * FROM equity WHERE ts>=? ORDER BY ts LIMIT ?", (iso(since), limit))
        else:
            rows = self._query("SELECT * FROM equity ORDER BY ts DESC LIMIT ?", (limit,))
            rows = list(reversed(rows))
        return [
            {
                "ts": r["ts"],
                "equity": D(r["equity"]),
                "cash": D(r["cash"]),
                "positions_value": D(r["positions_value"]),
                "realized": D(r["realized"]),
                "unrealized": D(r["unrealized"]),
                "fees": D(r["fees"]),
            }
            for r in rows
        ]

    def equity_at(self, ts: datetime) -> Decimal | None:
        rows = self._query("SELECT equity FROM equity WHERE ts<=? ORDER BY ts DESC LIMIT 1", (iso(ts),))
        return D(rows[0]["equity"]) if rows else None

    def last_equity(self) -> Decimal | None:
        rows = self._query("SELECT equity FROM equity ORDER BY id DESC LIMIT 1")
        return D(rows[0]["equity"]) if rows else None

    def peak_equity(self) -> Decimal | None:
        rows = self._query("SELECT MAX(CAST(equity AS REAL)) AS m FROM equity")
        return D(rows[0]["m"]) if rows and rows[0]["m"] is not None else None

    # --- kv (survives restart) -------------------------------------------
    def kv_set(self, key: str, value: Any, ts: datetime | None = None) -> None:
        self._exec(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, default=str), iso(ts or utcnow())),
        )

    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def kv_delete(self, key: str) -> None:
        self._exec("DELETE FROM kv WHERE key=?", (key,))

    def extend_flapping_hold(self, until: datetime, components: list[str], *, ts: datetime) -> None:
        """Atomically extend the hold; never overwrite malformed safety state."""
        with self.transaction() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key='health_flapping_hold'").fetchone()
            if row is not None:
                try:
                    existing = json.loads(row["value"])
                    previous = parse_iso(existing["until"])
                    if previous >= until:
                        return
                except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                    return
            conn.execute(
                "INSERT INTO kv(key,value,updated_at) VALUES('health_flapping_hold',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (json.dumps({"until": iso(until), "components": components}), iso(ts)),
            )

    # --- kill switch ------------------------------------------------------
    def set_kill_state(
        self,
        level: str,
        *,
        trigger: str = "",
        reason: str = "",
        actor: str = "system",
        cooldown_until: datetime | None = None,
        ts: datetime | None = None,
        only_escalate: bool = False,
        only_if_missing: bool = False,
        expected_state: dict[str, Any] | None = None,
        require_no_flapping_hold: bool = False,
        require_no_pending_incident: bool = False,
        track_incident: bool = False,
        transition_id_out: list[int] | None = None,
        incident_tracking_failed: bool = False,
    ) -> bool:
        now = ts or utcnow()
        with self.transaction() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key='kill_state'").fetchone()
            current = json.loads(row["value"]) if row else {"level": "L0"}
            previous = str(current.get("level", "L0"))
            if only_if_missing and row is not None:
                return False
            if expected_state is not None and current != expected_state:
                return False
            if only_escalate:
                ranks = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
                # Unknown/corrupt levels must never be downgraded.
                if level not in ranks or previous not in ranks or ranks[level] <= ranks[previous]:
                    return False
            if require_no_pending_incident and (
                current.get("incident_tracking_failed") or conn.execute(
                    "SELECT 1 FROM kv WHERE key GLOB 'kill_incident_pending:*' LIMIT 1"
                ).fetchone() is not None or conn.execute(
                    "SELECT 1 FROM kv WHERE key='ramp_incident_tracking_invalid'"
                ).fetchone() is not None
            ):
                return False
            if require_no_flapping_hold:
                hold_row = conn.execute("SELECT value FROM kv WHERE key='health_flapping_hold'").fetchone()
                if hold_row is not None:
                    try:
                        hold = json.loads(hold_row["value"])
                        if now <= parse_iso(hold["until"]):
                            return False
                    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                        return False
            event = conn.execute(
                """INSERT INTO kill_events(ts,level,previous,trigger,reason,actor,cooldown_until)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    iso(now),
                    level,
                    previous,
                    trigger,
                    reason,
                    actor,
                    iso(cooldown_until) if cooldown_until else None,
                ),
            )
            transition_id = int(event.lastrowid or 0)
            if track_incident:
                conn.execute(
                    "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?)",
                    (f"kill_incident_pending:{transition_id}", json.dumps({
                        "transition_id": transition_id, "level": level, "trigger": trigger,
                        "reason": reason, "actor": actor, "previous_level": previous, "ts": iso(now),
                    }), iso(now)),
                )
            conn.execute(
                "INSERT INTO kv(key,value,updated_at) VALUES('kill_state',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (
                    json.dumps(
                        {
                            "level": level,
                            # A unique transition identity prevents ABA: L1 -> L0 -> L1
                            # can otherwise look identical at the same clock instant.
                            "generation": event.lastrowid,
                            "trigger": trigger,
                            "reason": reason,
                            "actor": actor,
                            "ts": iso(now),
                            "cooldown_until": iso(cooldown_until) if cooldown_until else None,
                            "incident_tracking_failed": bool(
                                incident_tracking_failed or current.get("incident_tracking_failed")
                            ),
                        }
                    ),
                    iso(now),
                ),
            )
            conn.execute(
                "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                (
                    iso(now),
                    "CRIT" if level in ("L2", "L3") else "WARN",
                    "kill_switch",
                    actor,
                    json.dumps({"level": level, "previous": previous, "trigger": trigger, "reason": reason}),
                ),
            )
        if transition_id_out is not None:
            transition_id_out.append(transition_id)
        return True

    def has_pending_kill_incident(self) -> bool:
        return bool(self._query("SELECT 1 FROM kv WHERE key GLOB 'kill_incident_pending:*' LIMIT 1"))

    def has_invalid_ramp_tracking(self) -> bool:
        return bool(self._query("SELECT 1 FROM kv WHERE key='ramp_incident_tracking_invalid'"))

    def recover_pending_kill_incidents(self) -> int:
        """Replay committed escalation outbox records; never changes the kill level."""
        rows = self._query("SELECT value FROM kv WHERE key GLOB 'kill_incident_pending:*' ORDER BY key")
        recovered = 0
        for row in rows:
            pending = json.loads(row["value"])
            self.add_incident(
                pending["level"], pending["trigger"], pending["reason"],
                {"previous_level": pending["previous_level"], "actor": pending["actor"]},
                ts=parse_iso(pending["ts"]), kill_generation=pending["transition_id"],
            )
            recovered += 1
        return recovered

    def kill_state(self) -> dict[str, Any]:
        state = self.kv_get("kill_state", {"level": "L0"}) or {"level": "L0"}
        state.setdefault("level", "L0")
        return state

    def kill_level(self) -> str:
        return str(self.kill_state().get("level", "L0"))

    def kill_history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM kill_events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # --- incidents --------------------------------------------------------
    def add_incident(
        self,
        level: str,
        trigger: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        ts: datetime | None = None,
        *, kill_generation: int | None = None,
    ) -> int:
        """Persist an incident and its audit atomically; track critical ramp incidents.

        A corrupt ramp cannot be repaired here: record a durable invalid marker instead,
        so live ramp sizing remains blocked without suppressing the incident.
        """
        from ..risk.live_guards import RampController

        payload = payload or {}
        now = ts or utcnow()
        timestamp = iso(now)
        critical = level in ("L2", "L3", "CRIT")
        with self.transaction() as conn:
            if kill_generation is not None:
                key = f"kill_incident_pending:{kill_generation}"
                pending_row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
                if pending_row is None:
                    raise ValueError("kill incident is no longer pending")
                pending = json.loads(pending_row["value"])
                if (pending.get("transition_id") != kill_generation or pending.get("level") != level
                        or pending.get("trigger") != trigger or pending.get("reason") != summary):
                    raise ValueError("kill incident does not match the pending transition")
                payload = {"previous_level": pending["previous_level"], "actor": pending["actor"],
                           "kill_generation": kill_generation}
                timestamp = pending["ts"]
            cur = conn.execute(
                "INSERT INTO incidents(ts,level,trigger,summary,payload) VALUES(?,?,?,?,?)",
                (timestamp, level, trigger, summary, json.dumps(payload, default=str)),
            )
            incident_id = int(cur.lastrowid or 0)
            conn.execute(
                "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                (timestamp, "CRIT" if critical else "WARN", "incident", "system",
                 json.dumps({"level": level, "trigger": trigger, "summary": summary} | payload, default=str)),
            )
            if critical:
                try:
                    state = RampController._state_in_transaction(conn)
                except (ValueError, TypeError, OverflowError):
                    conn.execute(
                        "INSERT INTO kv(key,value,updated_at) VALUES('ramp_incident_tracking_invalid',?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                        (json.dumps({"incident_id": incident_id, "reason": "invalid ramp state"}), timestamp),
                    )
                    conn.execute(
                        "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                        (timestamp, "CRIT", "ramp_incident_tracking_invalid", "ramp",
                         json.dumps({"incident_id": incident_id, "trigger": trigger})),
                    )
                else:
                    state.critical_incidents += 1
                    conn.execute(
                        "INSERT INTO kv(key,value,updated_at) VALUES('ramp',?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                        (json.dumps(state.to_dict()), timestamp),
                    )
                    conn.execute(
                        "INSERT INTO events(ts,level,kind,actor,payload) VALUES(?,?,?,?,?)",
                        (timestamp, "WARN", "ramp_incident", "ramp", json.dumps({
                            "incident_id": incident_id, "stage": state.stage,
                            "critical_incidents": state.critical_incidents,
                        })),
                    )
            if kill_generation is not None:
                conn.execute("DELETE FROM kv WHERE key=?", (f"kill_incident_pending:{kill_generation}",))
        return incident_id

    def incidents(self, limit: int = 50, since: datetime | None = None) -> list[dict[str, Any]]:
        if since:
            rows = self._query(
                "SELECT * FROM incidents WHERE ts>=? ORDER BY id DESC LIMIT ?", (iso(since), limit)
            )
        else:
            rows = self._query("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,))
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "level": r["level"],
                "trigger": r["trigger"],
                "summary": r["summary"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    # --- experiments (backtest tracking, section 18.6) --------------------
    def save_experiment(
        self,
        run_id: str,
        *,
        kind: str,
        name: str = "",
        strategy: str = "",
        params: dict[str, Any] | None = None,
        data_hash: str = "",
        git_commit: str = "",
        split: str = "",
        seed: int | None = None,
        metrics: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        notes: str = "",
    ) -> None:
        self._exec(
            """INSERT OR REPLACE INTO experiments
               (run_id,created_at,kind,name,strategy,params,data_hash,git_commit,split,seed,metrics,artifacts,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                iso(utcnow()),
                kind,
                name,
                strategy,
                json.dumps(params or {}, default=str),
                data_hash,
                git_commit,
                split,
                seed,
                json.dumps(metrics or {}, default=str),
                json.dumps(artifacts or {}, default=str),
                notes,
            ),
        )

    def experiments(self, limit: int = 100, strategy: str | None = None) -> list[dict[str, Any]]:
        if strategy:
            rows = self._query(
                "SELECT * FROM experiments WHERE strategy=? ORDER BY created_at DESC LIMIT ?",
                (strategy, limit),
            )
        else:
            rows = self._query("SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            item = dict(r)
            for key in ("params", "metrics", "artifacts"):
                item[key] = json.loads(item[key]) if item[key] else {}
            out.append(item)
        return out

    def count_experiments(self, strategy: str) -> int:
        row = self._query("SELECT COUNT(*) AS c FROM experiments WHERE strategy=?", (strategy,))[0]
        return int(row["c"])

    def holdout_locked(self, strategy: str) -> bool:
        row = self._query("SELECT COUNT(*) AS c FROM holdout_uses WHERE strategy=?", (strategy,))[0]
        return int(row["c"]) > 0

    def record_holdout_use(
        self, strategy: str, run_id: str = "", override: bool = False, notes: str = ""
    ) -> None:
        self._exec(
            "INSERT INTO holdout_uses(ts,strategy,run_id,override,notes) VALUES(?,?,?,?,?)",
            (iso(utcnow()), strategy, run_id, int(override), notes),
        )

    # --- housekeeping -----------------------------------------------------
    def stats(self) -> dict[str, Any]:
        tables = ("events", "signals", "orders", "fills", "positions", "equity", "incidents")
        out: dict[str, Any] = {}
        for t in tables:
            out[t] = int(self._query(f"SELECT COUNT(*) AS c FROM {t}")[0]["c"])
        out["kill_level"] = self.kill_level()
        out["path"] = self.path
        return out


# --- (de)serialisation helpers -------------------------------------------
def _position_payload(pos: Position) -> dict[str, Any]:
    return {
        "symbol": pos.symbol,
        "strategy": pos.strategy,
        "qty": to_str(pos.qty),
        "avg_entry": to_str(pos.avg_entry),
        "stop": to_str(pos.stop),
        "target": to_str(pos.target),
        "initial_stop": to_str(pos.initial_stop),
        "opened_at": iso(pos.opened_at),
        "bars_held": pos.bars_held,
        "realized_pnl": to_str(pos.realized_pnl),
        "fees": to_str(pos.fees),
        "partial_taken": pos.partial_taken,
        "trailing_stop": to_str(pos.trailing_stop) if pos.trailing_stop is not None else None,
        "atr_at_entry": to_str(pos.atr_at_entry),
        "stop_order_id": pos.stop_order_id,
        "risk_per_unit": to_str(pos.risk_per_unit),
        "initial_qty": to_str(pos.initial_qty),
        "highest_price": to_str(pos.highest_price),
    }


def _position_from_row(row: sqlite3.Row) -> Position:
    p = json.loads(row["payload"])
    trailing = p.get("trailing_stop")
    return Position(
        symbol=p["symbol"],
        strategy=p["strategy"],
        qty=D(p["qty"]),
        avg_entry=D(p["avg_entry"]),
        stop=D(p["stop"]),
        target=D(p["target"]),
        initial_stop=D(p["initial_stop"]),
        opened_at=datetime.fromisoformat(p["opened_at"].replace("Z", "+00:00")),
        bars_held=int(p.get("bars_held", 0)),
        realized_pnl=D(p.get("realized_pnl", "0")),
        fees=D(p.get("fees", "0")),
        partial_taken=bool(p.get("partial_taken", False)),
        trailing_stop=D(trailing) if trailing else None,
        atr_at_entry=D(p.get("atr_at_entry", "0")),
        stop_order_id=p.get("stop_order_id"),
        risk_per_unit=D(p.get("risk_per_unit", "0")),
        initial_qty=D(p.get("initial_qty") or p["qty"]),
        highest_price=D(p.get("highest_price", "0")),
    )


def order_from_dict(data: dict[str, Any]) -> Order:
    return Order(
        client_order_id=data["client_order_id"],
        symbol=data["symbol"],
        side=Side(data["side"]),
        type=OrderType(data["type"]),
        qty=D(data["qty"]),
        price=D(data["price"]) if data.get("price") else None,
        status=OrderStatus(data["status"]),
        strategy=data.get("strategy", ""),
        signal_id=data.get("signal_id", ""),
        stop_price=D(data["stop_price"]) if data.get("stop_price") else None,
        time_in_force=TimeInForce(data.get("time_in_force", "GTC")),
        reduce_only=bool(data.get("reduce_only", False)),
        exchange_order_id=data.get("exchange_order_id"),
        filled_qty=D(data.get("filled_qty") or "0"),
        avg_fill_price=D(data.get("avg_fill_price") or "0"),
        fee=D(data.get("fee") or "0"),
        created_at=utc(datetime.fromisoformat(data["created_at"].replace("Z", "+00:00")))
        if data.get("created_at")
        else None,
    )
