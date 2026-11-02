"""Parity: recorded data replayed through the same engine must reproduce decisions.

The plan's parity test is the strongest anti-drift check available: record one day, run
it through the backtester and through the replay path, and require the *decisions* to
match event for event. If they do not, one of the two paths is reading the market
differently — and the backtest is no longer evidence about live behaviour.

What is compared here is the decision stream: signals emitted, risk verdicts, orders
submitted and positions opened. Fills are deliberately excluded from the strict
comparison: the paper adapter fills against an order book, and a candle-only recording
cannot reproduce book depth. `compare_fills` reports the difference for information, and
the docs say plainly that byte-identical fills need recorded books (see `data/archive`).
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from ..config import Config
from ..core.clock import SimClock
from ..core.models import Candle
from ..core.store import Store
from ..data.feed import ReplayFeed, SyntheticFeed, replay_from_market
from ..engine import Engine

#: The layer that a candle-only recording can reproduce exactly: what the strategies
#: saw, and what the risk engine said about it. These are pure functions of the candles.
DECISION_KINDS = ("signal", "risk_verdict")

#: Downstream of fills. A limit order fills when price *trades through* it, and that
#: needs ticks; a candle-only replay cannot know. Divergence here is reported, not
#: treated as failure - it is the documented limit of candle-only replay.
DOWNSTREAM_KINDS = ("order_submitted", "position_opened", "trade_closed")

FILL_KINDS = ("fill",)


@dataclass
class RecordedDay:
    """Everything needed to replay a session: the bars, and what the engine decided."""

    start: datetime
    end: datetime
    bars: dict[str, list[Any]] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def bar_count(self) -> int:
        return sum(len(v) for v in self.bars.values())

    def summary(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "symbols": sorted(self.bars),
            "bars": self.bar_count,
            "decisions": len(self.decisions),
            "fills": len(self.fills),
            **self.meta,
        }


def _event_rows(store: Store, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in store.events(limit=100_000):
        if event["kind"] in kinds:
            rows.append({"kind": event["kind"], "ts": str(event["ts"]), "payload": event.get("payload")})
    rows.sort(key=lambda r: (r["ts"], r["kind"]))
    return rows


def normalise(rows: list[dict[str, Any]], kinds: tuple[str, ...]) -> list[str]:
    """Flatten events into comparable strings, keeping only stable fields.

    Volatile fields (ids, latency, fee amounts that depend on book depth) are dropped on
    purpose: the question is "did it decide the same thing?", not "was every uuid equal".
    """
    out: list[str] = []
    for row in rows:
        if row["kind"] not in kinds:
            continue
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            payload = _maybe_json(payload)
        if row["kind"] == "signal":
            out.append(
                "signal|{ts}|{symbol}|{strategy}|{side}|{reason}".format(
                    ts=row["ts"][:19],
                    symbol=payload.get("symbol"),
                    strategy=payload.get("strategy"),
                    side=payload.get("side"),
                    reason=str(payload.get("reason", ""))[:60],
                )
            )
        elif row["kind"] == "risk_verdict":
            out.append(
                "risk|{ts}|{symbol}|{approved}|{reason}".format(
                    ts=row["ts"][:19],
                    symbol=payload.get("symbol"),
                    approved=payload.get("approved"),
                    reason=str(payload.get("reason", ""))[:60],
                )
            )
        elif row["kind"] == "order_submitted":
            out.append(
                "order|{ts}|{symbol}|{side}|{type}".format(
                    ts=row["ts"][:19],
                    symbol=payload.get("symbol"),
                    side=payload.get("side"),
                    type=payload.get("type"),
                )
            )
        elif row["kind"] == "position_opened":
            out.append(
                "opened|{ts}|{symbol}|{strategy}".format(
                    ts=row["ts"][:19],
                    symbol=payload.get("symbol"),
                    strategy=payload.get("strategy"),
                )
            )
        elif row["kind"] == "trade_closed":
            out.append(
                "closed|{ts}|{symbol}|{reason}".format(
                    ts=row["ts"][:19],
                    symbol=payload.get("symbol"),
                    reason=str(payload.get("exit_reason") or payload.get("reason") or "")[:40],
                )
            )
        elif row["kind"] == "fill":
            out.append(
                "fill|{ts}|{symbol}|{side}".format(
                    ts=row["ts"][:19],
                    symbol=payload.get("symbol"),
                    side=payload.get("side"),
                )
            )
    return out


def _maybe_json(value: str) -> dict[str, Any]:
    import json

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def compare(expected: list[str], actual: list[str]) -> dict[str, Any]:
    """Sequence comparison with the first divergence reported in context."""
    limit = min(len(expected), len(actual))
    first_diff: int | None = None
    for index in range(limit):
        if expected[index] != actual[index]:
            first_diff = index
            break
    if first_diff is None and len(expected) != len(actual):
        first_diff = limit

    window: dict[str, Any] = {}
    if first_diff is not None:
        lo = max(0, first_diff - 2)
        window = {
            "index": first_diff,
            "expected": expected[lo : first_diff + 3],
            "actual": actual[lo : first_diff + 3],
        }
    matches = first_diff is None
    return {
        "match": matches,
        "expected_count": len(expected),
        "actual_count": len(actual),
        "first_divergence": window or None,
    }


async def record_session(
    config: Config,
    *,
    start: datetime,
    minutes: int = 60,
    seed: int | None = None,
    capital: Decimal | None = None,
) -> RecordedDay:
    """Run the engine on the synthetic feed and keep the bars and its decisions."""
    cfg = copy.deepcopy(config)
    clock = SimClock(start)
    store = Store(":memory:")
    engine = await Engine.build(cfg, clock=clock, store=store)
    if capital is not None:
        engine.ledger.starting_cash = capital
        engine.adapter.cash = capital  # type: ignore[attr-defined]
        engine.starting_capital = capital
    if seed is not None and hasattr(engine.feed, "rng"):
        engine.feed.rng.seed(seed)

    end = start + timedelta(minutes=minutes)
    await engine.run(stop_at=end)

    bars: dict[str, list[Any]] = {}
    for symbol in cfg.symbol_names():
        recorded = replay_from_market(engine.market, symbol, engine.feed.base_timeframe)
        if recorded:
            bars[symbol] = recorded

    recorded_day = RecordedDay(
        start=start,
        end=end,
        bars=bars,
        decisions=_event_rows(store, DECISION_KINDS),
        fills=_event_rows(store, FILL_KINDS),
        meta={"seed": seed, "minutes": minutes, "source": "synthetic"},
    )
    engine.close()
    return recorded_day


async def replay_session(
    config: Config, recorded: RecordedDay, *, capital: Decimal | None = None
) -> RecordedDay:
    """Feed the recorded candles back through the engine, no synthetic data involved."""
    cfg = copy.deepcopy(config)
    clock = SimClock(recorded.start)
    feed = ReplayFeed(cfg, cast("dict[str, Sequence[Candle]]", recorded.bars), clock)
    store = Store(":memory:")
    engine = await Engine.build(cfg, clock=clock, store=store, feed=feed)
    if capital is not None:
        engine.ledger.starting_cash = capital
        engine.adapter.cash = capital  # type: ignore[attr-defined]
        engine.starting_capital = capital

    await engine.run(stop_at=recorded.end)

    result = RecordedDay(
        start=recorded.start,
        end=recorded.end,
        bars=recorded.bars,
        decisions=_event_rows(store, DECISION_KINDS),
        fills=_event_rows(store, FILL_KINDS),
        meta={**recorded.meta, "source": "replay"},
    )
    engine.close()
    return result


async def record_tape_session(
    config: Config,
    *,
    start: datetime,
    minutes: int = 60,
    seed: int | None = 7,
    capital: Decimal | None = None,
) -> tuple[Any, RecordedDay]:
    """Run a session with the feed recorded: bars, ticks and books, in order."""
    from ..data.tape import RecordingFeed

    cfg = copy.deepcopy(config)
    clock = SimClock(start)
    store = Store(":memory:")
    synthetic = SyntheticFeed(cfg, clock)
    if seed is not None:
        synthetic.rng.seed(seed)
    feed = RecordingFeed(cfg, synthetic, clock)
    engine = await Engine.build(cfg, clock=clock, store=store, feed=feed)
    if capital is not None:
        engine.ledger.starting_cash = capital
        engine.adapter.cash = capital  # type: ignore[attr-defined]
        engine.starting_capital = capital

    end = start + timedelta(minutes=minutes)
    await engine.run(stop_at=end)

    bars: dict[str, list[Any]] = {}
    for symbol in cfg.symbol_names():
        recorded = replay_from_market(engine.market, symbol, engine.feed.base_timeframe)
        if recorded:
            bars[symbol] = recorded

    day = RecordedDay(
        start=start,
        end=end,
        bars=bars,
        decisions=_event_rows(store, DECISION_KINDS + DOWNSTREAM_KINDS),
        fills=_event_rows(store, FILL_KINDS),
        meta={"seed": seed, "minutes": minutes, "source": "synthetic+tape"},
    )
    tape = feed.tape
    tape.meta = {"seed": seed, "minutes": minutes}
    engine.close()
    return tape, day


async def replay_tape_session(
    config: Config, tape: Any, *, capital: Decimal | None = None
) -> RecordedDay:
    """Emit the recorded tape back through the engine exactly as it arrived."""
    from ..data.tape import TapeFeed

    cfg = copy.deepcopy(config)
    clock = SimClock(tape.first_ts or datetime.now(UTC))
    feed = TapeFeed(cfg, tape, clock)
    store = Store(":memory:")
    engine = await Engine.build(cfg, clock=clock, store=store, feed=feed)
    if capital is not None:
        engine.ledger.starting_cash = capital
        engine.adapter.cash = capital  # type: ignore[attr-defined]
        engine.starting_capital = capital

    end = (tape.last_ts or clock.now()) + timedelta(minutes=1)
    await engine.run(stop_at=end)

    result = RecordedDay(
        start=tape.first_ts or clock.now(),
        end=end,
        bars={},
        decisions=_event_rows(store, DECISION_KINDS + DOWNSTREAM_KINDS),
        fills=_event_rows(store, FILL_KINDS),
        meta={**tape.meta, "source": "tape-replay"},
    )
    engine.close()
    return result


async def run_parity(
    config: Config,
    *,
    start: datetime,
    minutes: int = 60,
    seed: int | None = 7,
    capital: Decimal | None = None,
    mode: str = "tape",
) -> dict[str, Any]:
    """Record a session, replay it, and report whether the decisions agree.

    ``mode="tape"`` records the whole feed (bars, ticks, books) and replays it: decisions
    *and* fills must match exactly, because the replay sees the same orders arriving into
    the same book. ``mode="candles"`` records only closed candles, which is far cheaper to
    store but cannot reproduce a limit fill, so only the strategy/risk decisions are
    compared strictly.
    """
    if mode == "tape":
        tape, original = await record_tape_session(
            config, start=start, minutes=minutes, seed=seed, capital=capital
        )
        replayed = await replay_tape_session(config, tape, capital=capital)
        expected = normalise(original.decisions, DECISION_KINDS + DOWNSTREAM_KINDS)
        actual = normalise(replayed.decisions, DECISION_KINDS + DOWNSTREAM_KINDS)
        fills_expected = normalise(original.fills, FILL_KINDS)
        fills_actual = normalise(replayed.fills, FILL_KINDS)
        decision_result = compare(expected, actual)
        fill_result = compare(fills_expected, fills_actual)
        return {
            "mode": mode,
            "passed": decision_result["match"] and fill_result["match"],
            "decisions": decision_result,
            "expected_count": len(expected),
            "tape": tape.summary(),
            "recorded": original.summary(),
            "replayed": replayed.summary(),
            "fills": {"informational": fill_result, "note": "recorded books: fills must match"},
        }

    if mode != "candles":
        raise ValueError(f"unknown parity mode {mode!r}")

    original = await record_session(
        config, start=start, minutes=minutes, seed=seed, capital=capital
    )
    replayed = await replay_session(config, original, capital=capital)

    expected = normalise(original.decisions, DECISION_KINDS)
    actual = normalise(replayed.decisions, DECISION_KINDS)
    decision_result = compare(expected, actual)

    downstream_expected = normalise(original.decisions, DOWNSTREAM_KINDS)
    downstream_actual = normalise(replayed.decisions, DOWNSTREAM_KINDS)

    fills_expected = normalise(original.fills, FILL_KINDS)
    fills_actual = normalise(replayed.fills, FILL_KINDS)

    return {
        "mode": mode,
        "passed": decision_result["match"],
        "decisions": decision_result,
        "expected_count": len(expected),
        "recorded": original.summary(),
        "replayed": replayed.summary(),
        "downstream": {
            "informational": compare(downstream_expected, downstream_actual),
            "note": (
                "orders and exits follow fills, and a limit order fills only when price "
                "trades through it - candle-only replay cannot know that"
            ),
        },
        "fills": {
            "informational": compare(fills_expected, fills_actual),
            "note": (
                "candle-only replay cannot reproduce book depth; byte-identical fills need "
                "recorded books (data/archive)"
            ),
        },
    }


def format_parity(result: dict[str, Any]) -> str:
    decisions = result["decisions"]
    lines = [
        f"parity ({result.get('mode', 'tape')}): {'MATCH' if result['passed'] else 'DIVERGED'}",
        f"decisions: recorded {decisions['expected_count']} | replayed {decisions['actual_count']}",
    ]
    tape = result.get("tape")
    if tape:
        lines.append(
            f"tape: {tape['events']} events ({tape['bars']} bars, {tape['ticks']} ticks, "
            f"{tape['books']} books) | symbols {result['recorded']['symbols']}"
        )
    else:
        lines.append(f"bars {result['recorded']['bars']} | symbols {result['recorded']['symbols']}")

    fills = result.get("fills", {}).get("informational", {})
    if fills:
        lines.append(
            f"fills: {'match' if fills.get('match') else 'differ'} "
            f"({fills.get('expected_count', 0)} recorded)"
        )
    downstream = result.get("downstream", {}).get("informational")
    if downstream is not None:
        lines.append(
            f"downstream (orders/exits): {'match' if downstream.get('match') else 'differs'}"
            " - candle-only replay cannot reproduce limit fills"
        )
    if decisions["first_divergence"]:
        window = decisions["first_divergence"]
        lines.append(f"first divergence at index {window['index']}:")
        lines.append(f"  expected: {window['expected']}")
        lines.append(f"  actual:   {window['actual']}")
    return "\n".join(lines)
