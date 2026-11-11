"""Command line entry point.

    tradebot check                 validate every config file and print the effective settings
    tradebot backtest --days 30    deterministic synthetic backtest through the real engine
    tradebot run                   paper trading loop on the configured feed (Ctrl-C to stop)
    tradebot status                account, positions and kill-switch state
    tradebot report                build the daily report for the current day
    tradebot pause|resume|kill     manual kill-switch controls
    tradebot drill                 kill-switch fire drill in paper mode (time-to-flat measurement)
    tradebot dashboard             local read-only dashboard (FastAPI + HTML)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import Config, Mode, load_config
from .core.clock import Clock, SimClock, iso, parse_iso, utcnow
from .core.logging import get_logger, setup_logging
from .core.models import KillLevel
from .core.money import D, to_str
from .core.store import Store

log = get_logger("tradebot.main")


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
#: Where a synthetic backtest starts. Fixed so results are reproducible across machines.
SIM_ORIGIN = datetime(2024, 1, 1, tzinfo=UTC)
_ARCHIVE_BOUNDS: dict[str, tuple[datetime, datetime]] = {}


def _archive_bounds(cfg: Config) -> tuple[datetime, datetime] | None:
    """First and last archived bar, or None when this run is not replaying the archive."""
    if cfg.settings.feed.value != "archive":
        return None
    key = str(cfg.settings.archive_dir or "")
    cached = _ARCHIVE_BOUNDS.get(key)
    if cached is not None:
        return cached
    from .data.archive import read_candles

    bars = read_candles(cfg)  # cached by the loader: one read per process
    first = min(candle.ts for candles in bars.values() for candle in candles)
    last = max(candle.ts for candles in bars.values() for candle in candles)
    _ARCHIVE_BOUNDS[key] = (first, last)
    return (first, last)


def _sim_start(cfg: Config) -> datetime:
    """A replay starts where its data starts; everything else starts at the fixed origin."""
    bounds = _archive_bounds(cfg)
    return bounds[0] if bounds else SIM_ORIGIN


def _sim_end(cfg: Config, start: datetime, days: float) -> datetime:
    """The end of a simulated run: the requested window, clipped to the data that exists.

    Without the clip an archived run stops in the 2024 origin and finishes with zero trades,
    which is exactly the silent wrong answer this helper exists to prevent.
    """
    requested = start + timedelta(days=days)
    bounds = _archive_bounds(cfg)
    if bounds is None:
        return requested
    return min(requested, bounds[1] + timedelta(minutes=1))


def _config(args: argparse.Namespace) -> Config:
    overrides: dict[str, object] = {}
    risk_overrides: dict[str, object] = {}
    if getattr(args, "mode", None):
        overrides["mode"] = args.mode
    if getattr(args, "feed", None):
        overrides["feed"] = args.feed
    if getattr(args, "archive_root", None):
        overrides["archive_dir"] = args.archive_root
    if getattr(args, "speed", None):
        overrides["synthetic"] = {"speed": args.speed}
        # Order-rate limits are wall-clock on purpose. A synthetic feed running faster
        # than real time would trip them on replay volume alone, so scale the *limit*
        # rather than the clock: the engine stays honest and the demo stays tradable.
        speed = float(args.speed)
        if speed > 1:
            base = load_config(args.config_dir).risk
            risk_overrides = {
                "orders_per_minute": max(base.orders_per_minute, int(base.orders_per_minute * speed)),
                "orders_per_day": max(base.orders_per_day, int(base.orders_per_day * speed)),
            }
    if getattr(args, "min_target_cost_ratio", None) is not None:
        risk_overrides["min_target_cost_ratio"] = args.min_target_cost_ratio
    cfg = load_config(
        args.config_dir, overrides=overrides or None, risk_overrides=risk_overrides or None
    )
    level = "DEBUG" if getattr(args, "verbose", False) else cfg.settings.logging.level
    setup_logging(level, cfg.settings.logging.json_logs, cfg.settings.logging.file)
    return cfg


def _chain_filters(first: Any, second: Any) -> Any:
    """Both filters must agree. Each is veto-only, so chaining can only remove trades."""

    def chained(signal: Any, ctx: Any = None) -> tuple[bool, str]:
        allowed, reason = first(signal, ctx)
        if not allowed:
            return False, reason
        return second(signal, ctx)

    return chained


def _kill_number(status: dict[str, Any]) -> float:
    """Kill level as a number: MLflow metrics cannot be "L2", and the level is the point."""
    level = str((status.get("kill_state") or {}).get("level", "L0"))
    try:
        return float(level.lstrip("Ll") or 0)
    except ValueError:
        return 0.0


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


async def _build_engine(cfg: Config, *, store_path: str | None = None, sim: bool = False, start: datetime | None = None):
    from .alerts.notifier import Notifier
    from .alerts.router import router_from_config
    from .data.feed import SyntheticFeed
    from .engine import Engine

    clock = SimClock(start or utcnow()) if sim else Clock()
    store = Store(store_path or cfg.settings.db_path)
    feed = SyntheticFeed(cfg, clock) if cfg.settings.feed.value == "synthetic" else None
    # Every engine gets a delivery path now: disk first, then Telegram and the independent
    # webhook, with a retry queue. Before this, no CLI-built engine had a notifier at all,
    # so `alerting` was permanently DEGRADED and alerts went nowhere.
    notifier = Notifier(cfg, store, router=router_from_config(cfg, store))
    engine = await Engine.build(cfg, clock=clock, store=store, feed=feed, notifier=notifier)
    return engine


# ----------------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------------
def cmd_check(args: argparse.Namespace) -> int:
    cfg = _config(args)
    summary = {
        "mode": cfg.mode.value,
        "feed": cfg.settings.feed.value,
        "db": str(cfg.settings.db_path),
        "symbols": cfg.symbol_names(),
        "strategies": list(cfg.enabled_strategies),
        "starting_capital": str(cfg.settings.starting_capital_usdt),
        "max_capital": str(cfg.settings.max_capital_usdt),
        "risk_per_trade": str(cfg.risk.risk_per_trade),
        "secrets": cfg.secrets.redacted(),
        "live_guards": {
            "env_switch": cfg.secrets.live_trading_enabled,
            "max_age_days": cfg.settings.live_guards.go_nogo_max_age_days,
        },
    }
    _print(summary)
    print("\nconfig OK")
    return 0


async def _run_backtest(
    cfg: Config,
    days: int,
    seed: int | None = None,
    capital: Decimal | None = None,
    persist: str | None = None,
) -> dict:
    from .engine import Engine

    if cfg.mode is not Mode.PAPER:
        raise ValueError("backtests require paper mode")
    start = _sim_start(cfg)
    clock = SimClock(start)
    store = Store(persist) if persist else Store(":memory:")
    engine = await Engine.build(cfg, clock=clock, store=store)
    if capital is not None:
        engine.ledger.starting_cash = capital
        engine.ledger.cash = capital
        engine.adapter.cash = capital  # type: ignore[attr-defined]
        engine.starting_capital = capital
        store.kv_set("day_start_equity", to_str(capital))
        store.kv_set("week_start_equity", to_str(capital))
        store.kv_set("peak_equity", to_str(capital))
    if seed is not None and hasattr(engine.feed, "rng"):
        engine.feed.rng.seed(seed)  # a replay has no rng: its prices are already fixed
    await engine.run(stop_at=_sim_end(cfg, start, days))
    marks = engine.marks
    feed_info: dict[str, Any] = engine.feed.describe() if hasattr(engine.feed, "describe") else {}
    result: dict[str, Any] = {
        "mode": f"backtest({cfg.settings.feed.value})",
        "feed": cfg.settings.feed.value,
        "feed_detail": feed_info,
        "days": days,
        "sim_start": iso(start),
        "sim_end": iso(_sim_end(cfg, start, days)),
        "start_equity": to_str(engine.starting_capital, 2),
        "end_equity": to_str(engine.ledger.equity(marks), 2),
        "realized": to_str(engine.ledger.realized_pnl, 2),
        "fees": to_str(engine.ledger.fees_paid, 2),
        "open_positions": len(engine.ledger.open_positions),
        "closed_trades": len(engine.ledger.closed_trades),
        "win_rate": to_str(
            D(sum(1 for t in engine.ledger.closed_trades if t.pnl > 0)) / D(len(engine.ledger.closed_trades))
            if engine.ledger.closed_trades
            else D("0"),
            3,
        ),
        "stats": engine.stats.to_dict(),
        "order_stats": engine.om.stats.to_dict(),
        "kill_level": engine.killswitch.level.value,
    }
    report = await engine.emit_daily_report()
    result["report_paths"] = report["paths"]
    result["report_telegram"] = report["telegram"]
    result["trades"] = [t.to_dict() for t in engine.ledger.closed_trades[:20]]
    from .backtest.runner import evaluate

    evaluation = evaluate(store, cfg, engine.starting_capital)
    result["metrics"] = evaluation["metrics"].to_dict()
    result["monte_carlo"] = evaluation["monte_carlo"]
    result["gates"] = evaluation["gates"]
    result["passed"] = evaluation["passed"]
    result["summary"] = evaluation["summary"]
    return result


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = _config(args)
    persist = getattr(args, "persist", None)
    if persist:
        Path(persist).parent.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(
        _run_backtest(cfg, args.days, args.seed, D(args.capital) if args.capital else None, persist)
    )
    if args.json:
        _print(result)
    else:
        print(result["report_telegram"])
        print()
        print(f"closed trades: {result['closed_trades']} | win rate {result['win_rate']} | "
              f"fees {result['fees']} | end equity {result['end_equity']}")
        print("reports:", ", ".join(result["report_paths"].values()))
        if result.get("feed") == "archive":
            print(
                "data: recorded prices from the archive; depth is modelled from each bar's "
                "close, so slippage here is an assumption - calibrate it against a live run"
            )
    if result.get("gates"):
        from .backtest.metrics import format_gates

        print()
        print("validation gates:")
        print(format_gates(result["gates"]))
        print("verdict:", "PASS" if result["passed"] else "NOT PROMOTABLE")
    return 0


def cmd_live_check(args: argparse.Namespace) -> int:
    """Run every live-mode gate and print the result. Never trades."""
    from .risk.live_guards import LiveGuardChecker

    cfg = _config(args)
    store = Store(cfg.settings.db_path)
    checker = LiveGuardChecker(cfg, store)

    async def _go():
        adapter = None
        if args.with_adapter:
            from .core.clock import Clock
            from .data.hub import MarketData
            from .execution.adapters.ccxt_live import CcxtLiveAdapter

            market = MarketData(cfg.symbol_names())
            adapter = CcxtLiveAdapter(cfg, market, Clock(), testnet=cfg.mode is Mode.TESTNET)
            await adapter.connect()
        results = await checker.check(
            confirmation=args.confirm,
            expected_capital=cfg.settings.max_capital_usdt,
            adapter=adapter,
            feed_fresh=False,
            clock_drift_seconds=None,
        )
        if adapter is not None:
            await adapter.close()
        return results

    results = asyncio.run(_go())
    failed = [r for r in results if r.blocking and not r.passed]
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.name:26s} {r.detail}")
    print()
    print(f"{len(results) - len(failed)}/{len(results)} gates passed")
    store.close()
    return 1 if failed else 0


def cmd_venue_observe(args: argparse.Namespace) -> int:
    """Read a venue account snapshot without trading, reconciliation or reset authority."""
    from .data.hub import MarketData
    from .execution.adapters.ccxt_live import CcxtLiveAdapter
    from .execution.venue_comparison import compare_local
    from .execution.venue_observation import ObservationError, observe_account

    cfg = _config(args)
    if cfg.mode not in (Mode.LIVE, Mode.TESTNET):
        print("venue-observe requires --mode testnet or --mode live", file=sys.stderr)
        return 2

    async def _go() -> dict[str, Any]:
        clock = Clock()
        adapter = CcxtLiveAdapter(cfg, MarketData(cfg.symbol_names()), clock, testnet=cfg.mode is Mode.TESTNET)
        try:
            await adapter.connect()
            observation = await observe_account(adapter.exchange, mode=cfg.mode.value, clock=clock)
            if args.compare_local:
                started = clock.now()
                comparison = compare_local(observation, path=cfg.settings.db_path, quote=cfg.settings.quote_currency)
                comparison["local_read_started_at"] = iso(started)
                comparison["local_read_finished_at"] = iso(clock.now())
                observation["local_comparison"] = comparison
            return observation
        finally:
            await adapter.close()

    try:
        result = asyncio.run(_go())
    except ObservationError as exc:
        print(f"venue observation failed: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - do not expose credentials or raw venue responses
        print("venue observation failed: could not connect to venue", file=sys.stderr)
        return 1
    _print(result)
    return 0


def cmd_pending_fills(args: argparse.Namespace) -> int:
    """Inspect callback journal without modifying the database or authorizing recovery."""
    from .execution.fill_recovery import FillInspectionError, inspect_pending_fills

    cfg = _config(args)
    try:
        result = inspect_pending_fills(Path(cfg.settings.db_path))
    except FillInspectionError as exc:
        print(f"pending-fill inspection failed: {exc}", file=sys.stderr)
        return 1
    _print(result)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _config(args)
    if cfg.mode is Mode.LIVE:
        from .risk.live_guards import LiveGuardChecker

        store = Store(cfg.settings.db_path)
        checker = LiveGuardChecker(cfg, store)
        results = asyncio.run(
            checker.check(confirmation=getattr(args, "confirm", None), expected_capital=cfg.settings.max_capital_usdt)
        )
        for r in results:
            if not r.passed:
                print(f"FAIL  {r.name:26s} {r.detail}")
        try:
            checker.require(results)
        except Exception as exc:  # noqa: BLE001
            print(f"\nrefusing to start live: {exc}", file=sys.stderr)
            return 3
    engine = asyncio.run(_build_engine(cfg))
    if getattr(args, "meta_gate", None):
        from .meta_gate import attach

        try:
            entry_filter = attach(engine, args.meta_gate)
        except (OSError, ValueError) as exc:
            print(f"refusing to start with that meta model: {exc}", file=sys.stderr)
            engine.close()
            return 2
        print(
            f"meta gate: {entry_filter.gate.name} threshold {entry_filter.gate.threshold:.2f}, "
            f"{len(entry_filter.gate.features)} features (veto-only)"
        )
    if getattr(args, "ledger_gate", None):
        from .ai.ledger import LedgerEntryFilter, load_card

        guards = getattr(cfg.settings, "live_guards", None)
        max_age = float(getattr(guards, "go_nogo_max_age_days", 14) or 0)
        try:
            card = load_card(args.ledger_gate, max_age_days=max_age)
        except (OSError, ValueError) as exc:
            print(f"refusing to start with that ledger model: {exc}", file=sys.stderr)
            engine.close()
            return 2
        ledger_filter = LedgerEntryFilter(gate=card)
        existing = engine.entry_filter
        engine.entry_filter = (
            ledger_filter
            if existing is None
            else _chain_filters(existing, ledger_filter)
        )
        print(
            f"ledger gate: threshold {card.threshold:.2f}, "
            f"{len(card.vocabulary.strategies)} strategies x {len(card.vocabulary.symbols)} symbols, "
            f"trained on {card.meta.get('verdict', {}).get('summary', {}).get('trades', '?')} trades (veto-only)"
        )
        if card.stale_reason:
            print(f"ledger gate is inert: {card.stale_reason}", file=sys.stderr)

    news_poller = None
    if getattr(args, "news_url", None):
        from .news import attach as attach_news

        urls = [url.strip() for url in str(args.news_url).split(",") if url.strip()]
        news_poller = attach_news(engine, urls, interval_seconds=float(args.news_interval))
        print(f"sentiment: {len(urls)} feed(s), polled every {args.news_interval:.0f}s (reduce-only)")

    started_at = engine.clock.now()
    tracker = None
    if getattr(args, "mlflow_server", None):
        from .ai.tracking import LiveTracker

        tracker = LiveTracker(
            str(args.mlflow_server),
            experiment="tradebot-live",
            run_name=f"session-{started_at.strftime('%Y%m%dT%H%M%S')}",
        )
        tracker.start(
            params={"mode": cfg.mode.value, "feed": cfg.settings.feed.value,
                    "symbols": ",".join(cfg.symbol_names()),
                    "capital": cfg.settings.max_capital_usdt},
            tags={"kind": "live_session", "mode": cfg.mode.value},
        )
        if tracker.disabled:
            print(f"mlflow: live tracking off - {tracker.last_error or tracker.reason}")
        else:
            print(f"mlflow: streaming to {args.mlflow_server} as run {tracker.run_id}")

    async def _stream_metrics() -> None:
        """Push one point per interval so the curve exists while the session runs."""
        assert tracker is not None
        step = 0
        while True:
            await asyncio.sleep(max(5.0, float(getattr(args, "mlflow_interval", 60.0))))
            if tracker.disabled:
                return
            status = engine.status()
            stats = status.get("stats") or {}
            tracker.log(
                {
                    "equity": status.get("equity"),
                    "cash": status.get("cash"),
                    "realized_pnl": status.get("realized_pnl"),
                    "unrealized_pnl": status.get("unrealized_pnl"),
                    "fees": status.get("fees"),
                    "trades_closed": stats.get("trades_closed"),
                    "signals": stats.get("signals"),
                    "vetoes": stats.get("vetoes"),
                    "fills": stats.get("fills"),
                    "kill_level": _kill_number(status),
                },
                step=step,
            )
            step += 1

    async def _main() -> int:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        listener = _start_command_listener(cfg, engine)
        news_task = asyncio.create_task(news_poller.run(stop)) if news_poller is not None else None
        stream_task = (
            asyncio.create_task(_stream_metrics())
            if tracker is not None and not tracker.disabled
            else None
        )

        def _stop(*_: object) -> None:
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # pragma: no cover - windows
                loop.add_signal_handler(sig, _stop)
        stop_at = (
            engine.clock.now() + timedelta(minutes=float(args.minutes))
            if getattr(args, "minutes", None)
            else None
        )
        run_task = asyncio.create_task(engine.run(stop_at=stop_at))
        stop_task = asyncio.create_task(stop.wait())
        await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if not run_task.done():
            engine.running = False
            run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        for task in (listener, news_task, stream_task):
            if task is None:
                continue
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if news_poller is not None:
            print(f"news: {json.dumps(news_poller.stats.as_dict(), default=str)}")
        status = engine.status()
        print(json.dumps(status, indent=2, default=str))
        if getattr(args, "mlflow", None):
            from .ai.tracking import export_runs, session_run

            run = session_run(
                run_id=f"session-{started_at.strftime('%Y%m%dT%H%M%S')}",
                store=engine.store,
                status=status,
                started=started_at,
                mode=cfg.mode.value,
                capital=cfg.settings.max_capital_usdt,
                extra_params={"feed": cfg.settings.feed.value},
                extra_tags={"news_feeds": ",".join(news_poller.urls) if news_poller else ""},
            )
            report = export_runs([run], args.mlflow, name="tradebot-sessions")
            print(f"mlflow: wrote {report.runs} run to {args.mlflow} "
                  f"({report.metrics} metrics, {report.params} params, {report.tags} tags)")
        if tracker is not None:
            if not tracker.disabled:
                from .ai.tracking import session_run

                final = session_run(
                    run_id=tracker.run_id or tracker.run_name, store=engine.store, status=status,
                    started=started_at, mode=cfg.mode.value,
                    capital=cfg.settings.max_capital_usdt,
                )
                tracker.log(final["metrics"])
                tracker.finish()
                print(
                    f"mlflow: run {tracker.run_id} finished - {tracker.logged} points to "
                    f"{tracker.tracking_uri}"
                )
            if tracker.disabled:
                print(f"mlflow: live tracking stopped - {tracker.last_error or tracker.reason}")
        engine.close()
        return 0

    return asyncio.run(_main())


def cmd_metrics(args: argparse.Namespace) -> int:
    from .backtest.metrics import format_gates
    from .backtest.runner import evaluate

    cfg = _config(args)
    store = Store(cfg.settings.db_path)
    result = evaluate(store, cfg, D(args.capital) if args.capital else None)
    metrics = result["metrics"]
    if args.json:
        _print({"metrics": metrics.to_dict(), "monte_carlo": result["monte_carlo"],
                "gates": [g.to_dict() for g in result["gates"]], "passed": result["passed"]})
    else:
        print("\n".join(result["summary"]))
        print()
        print(f"monte carlo (bootstrap of trade order): p95 drawdown {result['monte_carlo']['p95_drawdown'] * 100:.2f}%, "
              f"median return {result['monte_carlo']['p50_return'] * 100:+.2f}%")
        print()
        print(format_gates(result["gates"]))
        print()
        print("verdict:", "PASS" if result["passed"] else "NOT PROMOTABLE (gates failed)")
    store.close()
    return 0


def cmd_autonomy(args: argparse.Namespace) -> int:
    """Show the partial autonomy audit ledger, without authorizing actions."""
    from .autonomy import AutonomyLedger

    cfg = _config(args)
    store = Store(cfg.settings.db_path)
    try:
        result = AutonomyLedger(store=store).summary()
    finally:
        store.close()
    _print(result)
    return 0


def cmd_recovery_request(args: argparse.Namespace) -> int:
    """Create a short-lived, state-bound request for offline operator signature."""
    from .signed_recovery import SignedFaultRecovery

    cfg = _config(args)
    store = Store(cfg.settings.db_path)
    try:
        result = SignedFaultRecovery(cfg, store).request(args.key_id, args.reason)
    finally:
        store.close()
    _print(result)
    return 0


def cmd_recovery_apply(args: argparse.Namespace) -> int:
    """Verify and consume an offline signature without resetting the kill switch."""
    from .signed_recovery import SignedFaultRecovery

    request_path, signature_path = Path(args.request), Path(args.signature)
    if request_path.stat().st_size > 16384 or signature_path.stat().st_size > 1024:
        raise ValueError("recovery input file is too large")
    request = json.loads(request_path.read_text(encoding="utf-8-sig"))
    signature = signature_path.read_text(encoding="utf-8-sig").strip()
    cfg = _config(args)
    store = Store(cfg.settings.db_path)
    try:
        SignedFaultRecovery(cfg, store).apply(request, signature)
        print("signature consumed; kill level unchanged; incident history remains unverified")
    finally:
        store.close()
    return 0


def cmd_allocation_request(args: argparse.Namespace) -> int:
    from .signed_allocation import SignedAllocation

    cfg = _config(args)
    store = Store(cfg.settings.db_path)
    try:
        _print(SignedAllocation(cfg, store).request(args.key_id, args.reason, args.cap))
    finally:
        store.close()
    return 0


def cmd_allocation_apply(args: argparse.Namespace) -> int:
    from .signed_allocation import SignedAllocation

    request_path, signature_path = Path(args.request), Path(args.signature)
    if request_path.stat().st_size > 16384 or signature_path.stat().st_size > 1024:
        raise ValueError("allocation input file is too large")
    request = json.loads(request_path.read_text(encoding="utf-8-sig"))
    signature = signature_path.read_text(encoding="utf-8-sig").strip()
    cfg = _config(args)
    store = Store(cfg.settings.db_path)
    try:
        SignedAllocation(cfg, store).apply(request, signature)
        print("allocation cap reduced; live readiness and kill level unchanged")
    finally:
        store.close()
    return 0


def cmd_strategy_reduction_request(args: argparse.Namespace) -> int:
    from .signed_strategy import SignedStrategyReduction

    cfg = _config(args)
    store = Store(cfg.settings.db_path)
    try:
        _print(SignedStrategyReduction(cfg, store).request(args.key_id, args.reason, args.strategy, args.multiplier))
    finally:
        store.close()
    return 0


def cmd_strategy_reduction_apply(args: argparse.Namespace) -> int:
    from .signed_strategy import SignedStrategyReduction

    request_path, signature_path = Path(args.request), Path(args.signature)
    if request_path.stat().st_size > 16384 or signature_path.stat().st_size > 1024:
        raise ValueError("strategy reduction input file is too large")
    request = json.loads(request_path.read_text(encoding="utf-8-sig"))
    signature = signature_path.read_text(encoding="utf-8-sig").strip()
    cfg = _config(args)
    store = Store(cfg.settings.db_path)
    try:
        SignedStrategyReduction(cfg, store).apply(request, signature)
        print("strategy size reduced; live readiness and kill level unchanged")
    finally:
        store.close()
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """One health check, printed: what each component answered and what the model made of it.

    Same code path as the dashboard's Health page and the daily report, so the three can
    never disagree about whether the feed is alive.
    """
    cfg = _config(args)

    async def _go() -> dict[str, Any]:
        engine = await _build_engine(cfg)
        try:
            health = await engine.health_check()
        finally:
            engine.close()
        return health

    health = asyncio.run(_go())
    if args.json:
        _print(health)
        return 0

    components: dict[str, Any] = health.get("components", {})
    print(f"overall: {components.get('overall', 'UNKNOWN')} | score {components.get('score', '?')}")
    if components.get("flapping"):
        print(f"flapping: {', '.join(components['flapping'])}")
    for name, info in (components.get("components") or {}).items():
        print(f"  {info['state']:8s} {name:11s} score {info['score']:.2f}")
        for check_name, check in (info.get("checks") or {}).items():
            failed = [
                label
                for label, ok in (
                    ("liveness", check["liveness"]),
                    ("readiness", check["readiness"]),
                    ("correctness", check["correctness"]),
                )
                if not ok
            ]
            suffix = f"  failed: {', '.join(failed)}" if failed else ""
            detail = f"  ({check['detail']})" if check.get("detail") else ""
            print(f"      {check_name}: {'ok' if not failed else 'no'}{suffix}{detail}")
    print()
    print(
        f"feed stale: {health.get('stale_symbols') or 'none'} | reconciliation "
        f"{health.get('reconciliation')} | disk {health.get('disk_used_pct', 0):.0%} | "
        f"error rate {health.get('error_rate', 0):.0%} | kill {health.get('kill_level')}"
    )
    playbook = health.get("playbook") or {}
    if playbook:
        print()
        print(f"heal playbook: {len(playbook.get('rows') or [])} row(s) wired, "
              f"breaker {playbook.get('breaker', {}).get('state', '?')}")
        for name, row in (playbook.get("metrics") or {}).items():
            p50 = row.get("p50_seconds")
            print(
                f"  {name:20s} attempts {row['attempts']:3d}  verified {row['verified']:3d}"
                f"  p50 {'n/a' if p50 is None else f'{p50:.1f}s'}"
                f"  target {row.get('target_seconds', '?')}s"
            )
        for name in playbook.get("unwired") or []:
            print(f"  {name:20s} declared, not wired (see docs/BUILD_PLAN_20_21.md)")
    return 0 if components.get("overall") != "FAILED" else 1


def cmd_alerts(args: argparse.Namespace) -> int:
    """Prove the alert path, or drain the retry queue.

    `--test` is the operator's answer to "would I actually be told?" - it writes the file,
    attempts every configured remote channel, and prints which one took it. If nothing did,
    the alert is queued, which is also the honest answer.
    """
    from .alerts.notifier import Notifier
    from .alerts.router import router_from_config

    cfg = _config(args)
    store = Store(cfg.settings.db_path)
    router = router_from_config(cfg, store)
    notifier = Notifier(cfg, store, router=router)

    async def _go() -> dict[str, Any]:
        if args.flush:
            delivered = await notifier.flush()
            return {"flushed": delivered, **router.as_dict()}
        text = args.text or (
            "tradebot alert test: if you can read this, the alert path works. "
            f"mode {cfg.mode.value}, {len(router.remote_channels)} remote channel(s)."
        )
        delivered = await notifier.send(text, level="WARN", key="alert-test")
        return {"delivered_remotely": delivered, **router.as_dict()}

    result = asyncio.run(_go())
    _print(result)
    if args.flush:
        return 0
    return 0 if result.get("delivered_remotely") else 3


def _supervisor_event(store: Store) -> Any:
    """Write supervisor decisions into the event log; return None (log_event returns int)."""

    def record(kind: str, payload: dict[str, Any]) -> None:
        store.log_event(
            "watchdog_supervisor", {"kind": kind, **payload}, level="WARN", actor="watchdog"
        )

    return record


def cmd_watchdog(args: argparse.Namespace) -> int:
    """Supervision: one pass, the loop, or a supervised restart of the bot.

    `--once` is for cron and for the game-day drill; `--supervise` is the long-running form
    that can actually restart the bot. In paper mode the preflight is a no-op (there is no
    venue to flatten), which is why the mode is printed with every result.
    """
    from .alerts.router import router_from_config
    from .watchdog.supervisor import ProcessSpawner, RestartBudget, Supervisor
    from .watchdog.watchdog import Watchdog

    cfg = _config(args)
    store = Store(cfg.settings.db_path)
    router = router_from_config(cfg, store)
    supervisor = None
    # The watchdog can only flatten what it can reach. Without a venue client it still
    # escalates and alerts. Live/testnet restart is blocked without verified preflight.
    actions = None
    if args.supervise:
        supervisor = Supervisor(
            spawner=ProcessSpawner(cwd=Path.cwd()),
            budget=RestartBudget(max_per_hour=cfg.settings.watchdog.restart_max_per_hour),
            verify_seconds=cfg.settings.watchdog.restart_verify_seconds,
            on_event=_supervisor_event(store),
        )
    watch = Watchdog(cfg, store, router=router, supervisor=supervisor, actions=actions)
    if args.interval:
        watch.interval = max(0.5, args.interval)

    async def _go() -> dict[str, Any]:
        if args.once:
            return await watch.act_async()
        stop = asyncio.Event()
        task = asyncio.create_task(watch.run(stop))
        try:
            while not task.done():
                await asyncio.sleep(1.0)
        finally:
            stop.set()
            await task
        return {"stopped": True}

    result = asyncio.run(_go())
    result["mode"] = cfg.mode.value
    result["deadman"] = watch.deadman.state()
    result["venue_client"] = (
        "none: cannot flatten; live/testnet restart blocked, paper restart only"
        if actions is None
        else "attached"
    )
    _print(result)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _config(args)
    store = Store(cfg.settings.db_path)
    _print(
        {
            "mode": cfg.mode.value,
            "kill_level": store.kill_level(),
            "kill_state": store.kill_state(),
            "positions": [p.to_dict() for p in store.open_positions()],
            "open_orders": store.open_orders(),
            "last_equity": str(store.last_equity()),
            "stats": store.stats(),
            "recent_events": store.events(limit=10),
        }
    )
    store.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = _config(args)

    async def _go() -> dict:
        engine = await _build_engine(cfg)
        out = await engine.emit_daily_report()
        engine.close()
        return out

    out = asyncio.run(_go())
    if args.json:
        _print(out["report"])
    else:
        print(out["telegram"])
        print()
        print("files:", ", ".join(out["paths"].values()))
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    cfg = _config(args)
    from .risk.killswitch import KillSwitch

    store = Store(cfg.settings.db_path)
    ks = KillSwitch(store, mode=cfg.mode)
    if args.action == "pause":
        ks.trigger(KillLevel.L1, trigger="manual", reason=args.reason or "manual pause", actor="cli")
    elif args.action == "kill":
        ks.trigger(KillLevel.L2, trigger="manual_kill", reason=args.reason or "manual kill", actor="cli")
    elif args.action == "lockdown":
        ks.trigger(KillLevel.L3, trigger="manual_lockdown", reason=args.reason or "manual lockdown", actor="cli")
    elif args.action == "resume":
        ks.reset(reason=args.reason or "manual resume", actor="cli")
    print(f"kill level is now {ks.level.value}")
    store.close()
    return 0


async def _drill(cfg: Config) -> dict:
    """Fire drill: open risk, then flatten, and measure time-to-flat (target < 10 s)."""
    from .engine import Engine

    start = datetime(2024, 1, 1, tzinfo=UTC)
    clock = SimClock(start)
    store = Store(":memory:")
    engine = await Engine.build(cfg, clock=clock, store=store)
    await engine.run(stop_at=start + timedelta(days=2))
    open_before = len(engine.ledger.open_positions)
    t0 = clock.monotonic()
    await engine.kill("fire drill: manual kill")
    await engine.run(max_events=200)
    elapsed = clock.monotonic() - t0
    return {
        "positions_before": open_before,
        "positions_after": len(engine.ledger.open_positions),
        "orders_cancelled": 0,
        "kill_level": engine.killswitch.level.value,
        "time_to_flat_seconds": round(elapsed, 3),
        "target_seconds": 10,
        "passed": len(engine.ledger.open_positions) == 0,
    }


def cmd_drill(args: argparse.Namespace) -> int:
    cfg = _config(args)
    _print(asyncio.run(_drill(cfg)))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    import uvicorn

    from .dashboard.app import create_app

    cfg = _config(args)
    app = create_app(cfg, run_bot=not getattr(args, "no_bot", False))
    # DASHBOARD_LOG_LEVEL=info turns on uvicorn's access log: the quickest way to tell
    # "the browser never reached us" apart from "we failed to answer".
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=os.environ.get("DASHBOARD_LOG_LEVEL", "warning"),
        access_log=os.environ.get("DASHBOARD_LOG_LEVEL", "").lower() == "info",
    )
    return 0


def _add_shared(parser: argparse.ArgumentParser) -> None:
    """The same flags are accepted before or after the sub-command."""
    parser.add_argument("--config-dir", default=argparse.SUPPRESS, help="config directory (default: config)")
    parser.add_argument("--mode", choices=[m.value for m in Mode], default=argparse.SUPPRESS)
    parser.add_argument(
        "--feed", choices=["synthetic", "ccxt", "stream", "archive"], default=argparse.SUPPRESS
    )
    parser.add_argument(
        "--archive-root", default=argparse.SUPPRESS, metavar="PATH",
        help="archive directory for `--feed archive` (default: <data_dir>/archive)",
    )
    parser.add_argument(
        "--speed", type=float, default=argparse.SUPPRESS,
        help="synthetic feed speed (simulated seconds per real second)",
    )
    parser.add_argument(
        "--min-target-cost-ratio", type=float, default=argparse.SUPPRESS, metavar="N",
        help="refuse entries whose target is nearer than N x the round-trip cost (0 disables)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradebot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.set_defaults(config_dir="config", verbose=False)
    _add_shared(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="validate configuration")

    bt = sub.add_parser("backtest", help="run a synthetic backtest through the real engine")
    bt.add_argument("--days", type=int, default=30)
    bt.add_argument("--seed", type=int, default=None)
    bt.add_argument("--capital", default=None)
    bt.add_argument("--json", action="store_true")
    bt.add_argument(
        "--persist", default=None, metavar="PATH",
        help="write the backtest to a real database instead of memory, so `learn`, "
             "`metrics` and `report` can read it afterwards",
    )

    run_parser = sub.add_parser("run", help="start the trading loop (paper by default)")
    run_parser.add_argument("--confirm", default=None, help='live mode: type the capital amount and "LIVE"')
    run_parser.add_argument(
        "--meta-gate", default=None, metavar="PATH",
        help="a saved meta-label model card; it may refuse entries, never resize them",
    )
    run_parser.add_argument(
        "--ledger-gate", default=None, metavar="PATH",
        help="a card written by `tradebot learn`: vetoes setups the bot's own trades lost on",
    )
    run_parser.add_argument(
        "--news-url", default=None, metavar="URL[,URL]",
        help="RSS feed(s) to score; sentiment can only shrink an entry, never grow it",
    )
    run_parser.add_argument("--news-interval", type=float, default=300.0, help="seconds between polls")
    run_parser.add_argument(
        "--mlflow", default=None, metavar="DIR",
        help="write the session to an MLflow file store on exit (read it back with "
             "`tradebot experiments --read DIR`)",
    )
    run_parser.add_argument(
        "--mlflow-server", default=None, metavar="URI",
        help="stream metrics to a running MLflow tracking server while the session runs "
             "(http://host:5000, sqlite:///path.db, or file:///abs/path); read them back "
             "with `tradebot experiments --read-server URI`",
    )
    run_parser.add_argument(
        "--mlflow-interval", type=float, default=60.0,
        help="seconds between live MLflow points (default 60)",
    )
    run_parser.add_argument(
        "--minutes", type=float, default=None, metavar="N",
        help="stop after N minutes of wall clock instead of running until interrupted "
             "(the engine clock in a live session is real time, so minutes are real minutes)",
    )

    venue_observe = sub.add_parser("venue-observe", help="read venue balances and open orders (not reset or safety evidence)")
    venue_observe.add_argument("--compare-local", action="store_true", help="diagnose differences against local DB (read-only)")

    sub.add_parser("pending-fills", help="inspect pending fill callbacks (read-only; not recovery evidence)")

    live_check = sub.add_parser("live-check", help="run every live-mode gate (never trades)")
    live_check.add_argument("--confirm", default=None)
    live_check.add_argument("--with-adapter", action="store_true", help="also verify exchange API permissions")
    sub.add_parser("status", help="show account and kill-switch state")

    rep = sub.add_parser("report", help="build the daily report now")
    rep.add_argument("--json", action="store_true")

    for name, help_text in (
        ("pause", "kill switch L1: no new entries"),
        ("resume", "reset the kill switch to L0"),
        ("kill", "kill switch L2: flatten everything"),
        ("lockdown", "kill switch L3: flatten + manual reset required"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--reason", default="")

    met = sub.add_parser("metrics", help="performance metrics and validation gates for the paper database")
    met.add_argument("--capital", default=None)
    met.add_argument("--json", action="store_true")

    sub.add_parser("drill", help="kill-switch fire drill in paper mode")

    dash = sub.add_parser("dashboard", help="run the local dashboard")
    dash.add_argument("--host", default="0.0.0.0")
    dash.add_argument("--port", type=int, default=8000)
    dash.add_argument("--no-bot", action="store_true", help="serve read-only, do not run the demo bot")

    wf = sub.add_parser("walk-forward", help="rolling out-of-sample windows (train then test)")
    wf.add_argument("--train-days", type=float, default=3.0)
    wf.add_argument("--test-days", type=float, default=1.0)
    wf.add_argument("--windows", type=int, default=4, help="cap the number of windows")
    wf.add_argument("--seed", type=int, default=None)
    wf.add_argument("--capital", default=None)
    wf.add_argument("--json", action="store_true")

    sens = sub.add_parser("sensitivity", help="parameter grid and plateau check")
    sens.add_argument("--strategy", default="S1_ema_trend")
    sens.add_argument("--param", action="append", default=[],
                      help="name=v1,v2,v3 (repeatable); defaults to the strategy's own range")
    sens.add_argument("--days", type=float, default=3.0)
    sens.add_argument("--metric", default="net_return", choices=["net_return", "profit_factor", "pnl"])
    sens.add_argument("--seed", type=int, default=None)
    sens.add_argument("--json", action="store_true")

    mt = sub.add_parser("meta", help="meta-labelling: gate rule-based candidates with a second model")
    mt.add_argument("--days", type=int, default=1)
    mt.add_argument("--seed", type=int, default=1)
    mt.add_argument("--horizon", type=int, default=30, help="forward horizon used by the screen")
    mt.add_argument("--max-bars", type=int, default=120, help="vertical barrier for labels")
    mt.add_argument("--json", action="store_true")

    slp = sub.add_parser("slippage", help="measure realised slippage and calibrate a model")
    slp.add_argument("--days", type=int, default=2)
    slp.add_argument("--seed", type=int, default=5)
    slp.add_argument("--json", action="store_true")

    sig = sub.add_parser("signals", help="feature store, signal library, IC screen, complexity ladder")
    sig.add_argument("--days", type=int, default=2)
    sig.add_argument("--seed", type=int, default=1)
    sig.add_argument("--horizon", type=int, default=4, help="forward horizon in bars")
    sig.add_argument("--folds", type=int, default=3, help="purged walk-forward windows")
    sig.add_argument("--min-ic", type=float, default=0.01)
    sig.add_argument("--save-model", default=None,
                     help="register a promoted ridge model as a card at this path (refused "
                          "unless the ladder promoted it)")
    sig.add_argument("--json", action="store_true")
    sig.add_argument(
        "--no-sequence", action="store_true",
        help="skip rung 4 (the window model); it is the slowest rung to fit",
    )

    rb = sub.add_parser("robustness", help="bootstrap, cost stress, benchmark and attribution")
    rb.add_argument("--days", type=int, default=10)
    rb.add_argument("--seed", type=int, default=5)
    rb.add_argument("--cost-multiplier", type=float, default=2.0)
    rb.add_argument("--json", action="store_true")

    arc = sub.add_parser("archive", help="Parquet candle/book archive (write a session, inspect, prune)")
    arc.add_argument("--root", default=None, help="archive directory (default: <data_dir>/archive)")
    arc.add_argument("--record-minutes", type=int, default=0, help="record a session of this length")
    arc.add_argument("--seed", type=int, default=7)
    arc.add_argument("--keep-days", type=int, default=None, help="delete book partitions older than this")
    arc.add_argument("--json", action="store_true")

    rs = sub.add_parser(
        "research", help="bounded search over a declared space, with every attempt counted"
    )
    rs.add_argument("--strategy", default="S1_ema_trend")
    rs.add_argument("--days", type=int, default=4, help="total window the stages divide up")
    rs.add_argument("--budget", type=int, default=16, help="cap on candidates (each is a trial)")
    rs.add_argument("--steps", type=int, default=3, help="values per parameter when auto-building")
    rs.add_argument("--spread", type=float, default=0.5, help="how far around each configured value")
    rs.add_argument("--keep", type=float, default=0.5, help="fraction that survives each stage")
    rs.add_argument("--stage", type=float, action="append", default=None,
                    help="stage as a fraction of the window; repeatable (default 0.25, 0.5, 1.0)")
    rs.add_argument("--space", action="append", default=None, metavar="PARAM=V1,V2",
                    help="explicit axis, e.g. --space ema_fast=12,20 (repeatable)")
    rs.add_argument("--seed", type=int, default=1)
    rs.add_argument("--json", action="store_true")

    nws = sub.add_parser("news", help="score an RSS feed once and show the size multiplier")
    nws.add_argument("--url", default=None, help="RSS URL(s), comma separated")
    nws.add_argument("--file", default=None, help="read the RSS from a file instead (offline)")
    nws.add_argument("--symbols", default=None, help="default: the configured symbols")
    nws.add_argument("--limit", type=int, default=10)
    nws.add_argument("--json", action="store_true")

    learn = sub.add_parser("learn", help="learn from the bot's own closed trades (veto-only)")
    learn.add_argument("--windows", type=int, default=3, help="purged walk-forward windows")
    learn.add_argument("--min-trades", type=int, default=200, help="sample floor before a model gets a say")
    learn.add_argument("--save", default=None, metavar="PATH", help="write the card (only if promoted)")
    learn.add_argument("--json", action="store_true")
    learn.add_argument("--db", default=None, metavar="PATH", help="read this database instead of the paper one")
    learn.add_argument(
        "--inspect", default=None, metavar="PATH",
        help="describe a saved card (age, threshold, training size) and say whether it is still valid",
    )

    exp = sub.add_parser("experiments", help="the experiment log, and an MLflow export of it")
    exp.add_argument("--limit", type=int, default=200)
    exp.add_argument("--strategy", default=None)
    exp.add_argument("--export", default=None, metavar="DIR", help="write an MLflow file store here")
    exp.add_argument("--json", action="store_true")
    exp.add_argument("--read", default=None, metavar="DIR",
                     help="read an MLflow file store back: what a teammate's runs contain")
    exp.add_argument("--read-server", default=None, metavar="URI",
                     help="read runs back from a tracking server (http://, sqlite://, file://)")

    bfl = sub.add_parser("backfill", help="paginate historical candles into the archive")
    bfl.add_argument("--symbols", default=None, help="comma-separated; default: config symbols")
    bfl.add_argument("--timeframe", default="1m")
    bfl.add_argument("--days", type=int, default=3)
    bfl.add_argument("--from", dest="from_dt", default=None, help="ISO start (overrides --days)")
    bfl.add_argument("--to", dest="to_dt", default=None, help="ISO end (default: now)")
    bfl.add_argument("--source", choices=["ccxt", "demo"], default="ccxt")
    bfl.add_argument("--page-limit", type=int, default=500)
    bfl.add_argument("--max-pages", type=int, default=500)
    bfl.add_argument("--root", default=None, help="archive directory")
    bfl.add_argument("--write", action="store_true", help="write the candles to the archive")
    bfl.add_argument("--seed", type=int, default=7)
    bfl.add_argument("--json", action="store_true")

    sub.add_parser("autonomy", help="show the partial autonomy audit ledger")

    rec_req = sub.add_parser("recovery-request", help="create a signed incident-tracking acknowledgement request")
    rec_req.add_argument("--key-id", required=True, help="SHA-256 fingerprint of an authorized public key")
    rec_req.add_argument("--reason", required=True, help="written reason for acknowledgement")
    rec_apply = sub.add_parser("recovery-apply", help="consume an offline signed incident acknowledgement")
    rec_apply.add_argument("--request", required=True, help="request JSON file")
    rec_apply.add_argument("--signature", required=True, help="base64 Ed25519 signature file")

    alloc_req = sub.add_parser("allocation-request", help="request a signed reduction of the live allocation cap")
    alloc_req.add_argument("--key-id", required=True)
    alloc_req.add_argument("--reason", required=True)
    alloc_req.add_argument("--cap", required=True, help="new cap as a decimal fraction")
    alloc_apply = sub.add_parser("allocation-apply", help="consume a signed live allocation cap reduction")
    alloc_apply.add_argument("--request", required=True)
    alloc_apply.add_argument("--signature", required=True)

    strat_req = sub.add_parser("strategy-reduction-request", help="request a signed strategy size reduction")
    strat_req.add_argument("--key-id", required=True)
    strat_req.add_argument("--reason", required=True)
    strat_req.add_argument("--strategy", required=True)
    strat_req.add_argument("--multiplier", required=True, help="new multiplier as a decimal fraction")
    strat_apply = sub.add_parser("strategy-reduction-apply", help="consume a signed strategy size reduction")
    strat_apply.add_argument("--request", required=True)
    strat_apply.add_argument("--signature", required=True)

    hlth = sub.add_parser("health", help="run one health check and print component state")
    hlth.add_argument("--json", action="store_true")

    alr = sub.add_parser("alerts", help="prove the alert path, or drain the retry queue")
    alr.add_argument("--test", action="store_true", help="send one test alert (default action)")
    alr.add_argument("--flush", action="store_true", help="retry queued alerts now")
    alr.add_argument("--text", default=None, help="custom alert text")
    alr.add_argument("--json", action="store_true")

    wdp = sub.add_parser("watchdog", help="supervision: one pass or the supervised loop")
    wdp.add_argument("--once", action="store_true", help="one pass, then exit")
    wdp.add_argument(
        "--supervise",
        action="store_true",
        help="restart the bot process when the heartbeat is lost (budget: 3/hour)",
    )
    wdp.add_argument("--interval", type=float, default=None, help="seconds between passes")
    wdp.add_argument("--json", action="store_true")

    bfh = sub.add_parser(
        "backfill-history",
        help="fill the archive month by month, resuming where the last run stopped",
    )
    bfh.add_argument("--months", type=int, default=6, help="how many months back from --to")
    bfh.add_argument("--symbols", default=None, help="comma-separated; default: config symbols")
    bfh.add_argument("--timeframe", default="1m")
    bfh.add_argument("--from", dest="from_dt", default=None, help="ISO start (overrides --months)")
    bfh.add_argument("--to", dest="to_dt", default=None, help="ISO end (default: now)")
    bfh.add_argument("--source", choices=["ccxt", "demo"], default="ccxt")
    bfh.add_argument("--page-limit", type=int, default=1000)
    bfh.add_argument("--max-pages", type=int, default=1000)
    bfh.add_argument("--root", default=None, help="archive directory")
    bfh.add_argument(
        "--state",
        default=None,
        help="resume file (default: <data_dir>/backfill-history.json)",
    )
    bfh.add_argument("--retry-incomplete", action="store_true", help="re-fetch chunks that were not complete")
    bfh.add_argument("--seed", type=int, default=7, help="offline demo exchange only")
    bfh.add_argument("--json", action="store_true")

    tlg = sub.add_parser("telegram", help="inbound Telegram command listener (bounded run)")
    tlg.add_argument("--polls", type=int, default=1, help="how many long polls to perform")
    tlg.add_argument("--poll-timeout", type=int, default=1, help="long-poll timeout in seconds")
    tlg.add_argument("--json", action="store_true")

    stm = sub.add_parser("stream", help="streaming feed: replay a recording, or try the live socket")
    stm.add_argument("--minutes", type=int, default=120, help="minutes of synthetic market to record")
    stm.add_argument("--seed", type=int, default=7)
    stm.add_argument("--live", action="store_true", help="try the real socket instead of a replay")
    stm.add_argument("--seconds", type=float, default=10.0, help="how long to keep the live socket open")
    stm.add_argument("--json", action="store_true")

    ven = sub.add_parser("venues", help="market calendar, square-off and cost comparison")
    ven.add_argument("--venue", default="crypto", choices=["crypto", "nse"])
    ven.add_argument("--notional", default="100000", help="notional for the cost comparison")
    ven.add_argument("--at", default=None, help="ISO timestamp to evaluate (default: now)")
    ven.add_argument("--json", action="store_true")

    par = sub.add_parser("parity", help="record a session, replay it, compare decisions")
    par.add_argument("--minutes", type=int, default=90)
    par.add_argument("--seed", type=int, default=7)
    par.add_argument("--json", action="store_true")

    png = sub.add_parser(
        "dashboard-png", help="render the dashboard to a PNG (no browser needed)"
    )
    png.add_argument("--url", default="http://127.0.0.1:8000/api/snapshot")
    png.add_argument("--out", default="reports/dashboard.png")

    for action in sub.choices.values():
        _add_shared(action)
    return parser


def cmd_walk_forward(args: argparse.Namespace) -> int:
    from .backtest.walkforward import format_walk_forward, rolling_windows, run_walk_forward

    cfg = _config(args)
    start = _sim_start(cfg)
    total = args.train_days + args.test_days * max(1, args.windows)
    windows = rolling_windows(
        start,
        _sim_end(cfg, start, total),
        train_days=args.train_days,
        test_days=args.test_days,
    )[: max(1, args.windows)]
    if not windows:
        print("no windows fit in the requested range", file=sys.stderr)
        return 2
    capital = D(args.capital) if args.capital else None
    result = asyncio.run(
        run_walk_forward(cfg, windows, capital=capital, seed=args.seed, cost_stress=True)
    )
    if args.json:
        _print(
            {
                "windows": result["windows"],
                "summary": result["summary"],
                "metrics": result["metrics_dict"],
                "cost_stress": result["cost_stress"],
                "gates": [{"name": g.name, "passed": g.passed, "detail": g.detail} for g in result["gates"]],
                "passed": result["passed"],
            }
        )
    else:
        print(format_walk_forward(result))
        print()
        from .backtest.metrics import format_gates

        print(format_gates(result["gates"]))
        print("verdict:", "PASS" if result["passed"] else "NOT PROMOTABLE")
    return 0


def cmd_sensitivity(args: argparse.Namespace) -> int:
    from .backtest.sensitivity import format_sensitivity, run_grid

    cfg = _config(args)
    axes = _parse_axes(args.param, cfg, args.strategy)
    if not axes:
        print("no parameters to sweep", file=sys.stderr)
        return 2
    start = _sim_start(cfg)
    report = asyncio.run(
        run_grid(
            cfg,
            strategy=args.strategy,
            axes=axes,
            start=start,
            end=_sim_end(cfg, start, args.days),
            seed=args.seed,
            metric=args.metric,
        )
    )
    # Every cell counts as an attempt. Log them, then deflate the best one.
    from .backtest.robustness import log_trials, multiple_testing_report, trials_from_store
    from .core.store import Store

    store = Store(cfg.settings.db_path)
    try:
        log_trials(store, args.strategy, report["grid"], metric=args.metric, seed=args.seed)
        trials = trials_from_store(store, args.strategy)
        total_for_strategy = store.count_experiments(args.strategy)
    finally:
        store.close()
    testing = multiple_testing_report(trials, observations=max(1, args.days * 180))
    testing["logged_total_for_strategy"] = total_for_strategy
    testing["units"] = (
        "sharpe" if args.metric == "sharpe" else f"{args.metric} (deflation needs a Sharpe metric to be meaningful)"
    )
    report["multiple_testing"] = testing

    if args.json:
        _print(report)
    else:
        print(format_sensitivity(report))
        print()
        print(f"multiple testing: {testing['trials']} trials logged for {args.strategy} "
              f"({total_for_strategy} rows in the experiment log)")
        print(f"  {testing['verdict']}")
        print()
        header = " | ".join(axes)
        print(f"{header} | score | trades")
        for row in report["grid"]:
            values = " | ".join(str(row.get(name)) for name in axes)
            print(f"{values} | {row.get('score')} | {row.get('trades')}")
    return 0


def _parse_axes(specs: list[str], cfg: Any, strategy: str) -> dict[str, list[Any]]:
    """``--param ema_fast=12,20,30`` -> ``{"ema_fast": [12, 20, 30]}``."""
    axes: dict[str, list[Any]] = {}
    for spec in specs:
        name, _, values = spec.partition("=")
        if not name or not values:
            raise SystemExit(f"bad --param {spec!r}; expected name=v1,v2")
        raw = [v.strip() for v in values.split(",") if v.strip()]
        axes[name] = [_coerce(v) for v in raw]
    if axes:
        return axes
    # A sensible default sweep around whichever strategy was asked for.
    entry = cfg.strategies.strategies.get(strategy)
    if entry is None:
        raise SystemExit(f"unknown strategy {strategy!r}")
    for name in ("ema_fast", "ema_slow", "adx_min", "bb_stdev"):
        if name not in entry.params:
            continue
        center = entry.params[name]
        try:
            base = float(center)
        except (TypeError, ValueError):
            continue
        step = max(1.0, round(base * 0.3))
        sweep = sorted({base - step, base, base + step})
        axes[name] = [int(v) if float(v).is_integer() else v for v in sweep]
    return axes


def _coerce(text: str) -> Any:
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


async def _run_robustness(cfg: Config, days: int, seed: int | None, multiplier: float) -> dict:
    """One backtest at nominal costs, then the same backtest with costs multiplied."""
    from .backtest.robustness import robustness_report, stress_config
    from .backtest.runner import evaluate
    from .core.clock import SimClock
    from .core.store import Store
    from .engine import Engine

    if cfg.mode is not Mode.PAPER:
        raise ValueError("robustness runs require paper mode")

    async def _session(this_cfg: Config) -> tuple[Any, Any, Any]:
        start = _sim_start(this_cfg)
        clock = SimClock(start)
        store = Store(":memory:")
        engine = await Engine.build(this_cfg, clock=clock, store=store)
        if seed is not None and hasattr(engine.feed, "rng"):
            engine.feed.rng.seed(seed)
        await engine.run(stop_at=_sim_end(this_cfg, start, days))
        # not closed here: evaluate() reads the store, and closing it first is how this
        # command failed the first time ("Cannot operate on a closed database")
        return store, engine, start

    store, engine, _start = await _session(cfg)
    base = evaluate(store, cfg, engine.starting_capital)
    curve, trades, _exposure = _collect(store)
    closes = _closes_from(engine)
    engine.close()
    store.close()

    stressed_store, stressed_engine, _ = await _session(stress_config(cfg, multiplier))
    stressed_eval = evaluate(stressed_store, cfg, stressed_engine.starting_capital)
    stressed_engine.close()
    stressed_store.close()

    report = robustness_report(
        curve=curve,
        trades=trades,
        starting_equity=engine.starting_capital,
        cost_stress_metrics=stressed_eval["metrics"],
        cost_multiplier=multiplier,
        closes=closes,
        seed=seed or 7,
    )
    report["nominal"] = {
        "return_pct": float(base["metrics"].total_return) * 100.0,
        "profit_factor": float(base["metrics"].profit_factor),
        "sharpe": float(base["metrics"].sharpe),
        "trades": base["metrics"].trade_count,
    }
    report["stressed"] = {
        "return_pct": float(stressed_eval["metrics"].total_return) * 100.0,
        "profit_factor": float(stressed_eval["metrics"].profit_factor),
        "sharpe": float(stressed_eval["metrics"].sharpe),
        "trades": stressed_eval["metrics"].trade_count,
    }
    # A real second engine run is available here. It is modeled, not proof of
    # untouched OOS data or venue costs, and only 2x satisfies the required gate.
    from .backtest.metrics import acceptance_gates, all_gates_passed

    stress_gate_metrics = stressed_eval["metrics"] if multiplier == 2.0 else None
    gates = acceptance_gates(base["metrics"], mc=base["monte_carlo"], cost_stress_metrics=stress_gate_metrics)
    report["gates"] = [g.to_dict() for g in gates]
    report["passed"] = all_gates_passed(gates)
    report["stress_model"] = ("modeled fees and adverse market slippage scaled; limit fills stay at limit; "
                              "venue costs and out-of-sample provenance unverified")
    return report


def _collect(store: Any) -> tuple[Any, Any, Any]:
    from .backtest.runner import collect

    return collect(store)


def _closes_from(engine: Any) -> dict[str, list[float]]:
    """Closing prices per symbol, for the buy-and-hold benchmark."""
    # closes per symbol from the 1m bars the engine still holds, for the buy-and-hold
    # benchmark. Read straight off the market hub: `MarketData.candles(symbol, timeframe)`.
    closes: dict[str, list[float]] = {}
    market = engine.market
    for symbol in market.symbols:
        series = market.candles(symbol, market.base_timeframe)
        values = [float(candle.close) for candle in series.last(market.series_maxlen)]
        if len(values) >= 2:
            closes[symbol] = values
    return closes


async def _collect_frames(cfg: Config, days: int, seed: int | None) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Run a session with recording on, then build a feature frame per symbol.

    The bars come from the engine's own market hub and the books from the recorded tape,
    so the features see exactly what a live process would have seen.
    """
    from .ai.features import FeatureStore
    from .core.clock import SimClock
    from .core.store import Store
    from .data.feed import SyntheticFeed
    from .data.tape import RecordingFeed
    from .engine import Engine

    start = datetime(2024, 1, 1, tzinfo=UTC)
    clock = SimClock(start)
    store = Store(":memory:")
    synthetic = SyntheticFeed(cfg, clock)
    if seed is not None:
        synthetic.rng.seed(seed)
    feed = RecordingFeed(cfg, synthetic, clock)
    engine = await Engine.build(cfg, clock=clock, store=store, feed=feed)
    await engine.run(stop_at=start + timedelta(days=days))

    series: dict[str, list[Any]] = {}
    for symbol in engine.market.symbols:
        candles = engine.market.candles(symbol, engine.market.base_timeframe).last(
            engine.market.series_maxlen
        )
        if len(candles) >= 100:
            series[symbol] = candles
    books: dict[str, dict[datetime, Any]] = {}
    for kind, payload, _stamp in feed.tape.events:
        if kind == "book":
            books.setdefault(payload.symbol, {})[payload.ts] = payload
    engine.close()
    store.close()
    store_choice = None
    frames = FeatureStore().compute_many(series, books=books)
    return store_choice, frames, {"symbols": len(frames), "books": sum(len(b) for b in books.values())}


def cmd_signals(args: argparse.Namespace) -> int:
    """Feature store -> signal library -> IC screen -> complexity ladder, end to end."""
    from .ai.ladder import (
        feature_importance,
        run_ladder,
        shuffled_label_test,
        time_shift_test,
    )
    from .ai.signals import SIGNALS, latest_scores, screen

    cfg = _config(args)
    _unused, frames, meta = asyncio.run(_collect_frames(cfg, args.days, args.seed))
    if not frames:
        print("no symbols had enough bars to build features", file=sys.stderr)
        return 2

    per_symbol: dict[str, Any] = {}
    for symbol, frame in frames.items():
        result = screen(frame, primary_horizon=args.horizon, min_ic=args.min_ic)
        ladder = run_ladder(
            frame, horizon=args.horizon, n_folds=args.folds, include_sequence=not args.no_sequence
        )
        per_symbol[symbol] = {
            "bars": len(frame),
            "features": len(frame.features),
            "kept": [spec.name for spec in result.kept],
            "dropped": result.dropped,
            "shuffled_ic": round(result.shuffled_ic, 5),
            "table": result.table_rows(),
            "ladder": ladder.as_dict(),
            "leakage": {
                "shuffled_labels": shuffled_label_test(frame, horizon=args.horizon),
                "time_shift": time_shift_test(frame, horizon=args.horizon),
            },
            "importance": feature_importance(frame, horizon=args.horizon)[:10],
            "latest": latest_scores(frame, result.kept)[:5],
        }

    report: dict[str, Any] = {
        "library_size": len(SIGNALS),
        "families": sorted({spec.family for spec in SIGNALS}),
        "horizon_bars": args.horizon,
        "days": args.days,
        "seed": args.seed,
        "meta": meta,
        "symbols": per_symbol,
    }
    if args.save_model:
        report["saved_model"] = _save_promoted_model(
            frames, horizon=args.horizon, path=args.save_model
        )

    if args.json:
        _print(report)
        return 0

    saved = report.get("saved_model")
    if saved:
        if saved.get("saved"):
            print(f"model card written to {saved['path']}")
        else:
            print(f"no model registered: {saved['reason']}")
        print()
    print(f"signal library: {len(SIGNALS)} signals across {len(report['families'])} families "
          f"({', '.join(report['families'])})")
    print(f"data: {meta['symbols']} symbols, {meta['books']} recorded books, "
          f"{args.days} simulated days, horizon {args.horizon} bars")
    for symbol, data in per_symbol.items():
        print()
        print(f"{symbol}: {data['bars']} bars, {data['features']} features, "
              f"kept {len(data['kept'])} of {len(data['table'])} signals")
        samples = data["table"][0]["non_overlapping_samples"] if data["table"] else 0
        band = data["table"][0]["noise_band"] if data["table"] else 0.0
        print(f"  IC noise band: +/-{band:.3f} at {samples} non-overlapping samples "
              f"(horizon {args.horizon})")
        for row in data["table"][:6]:
            mark = "keep" if row["kept"] else "drop"
            proof = "sig" if row["distinguishable"] else "thin"
            print(f"  {mark} {proof:4s} {row['name']:28s} IC {row['ic']:+.4f} | "
                  f"net/bar {row['net_pnl']:+.6f} | hit {row['hit_rate']:.2f} | turn {row['turnover']:.2f}")
        if not data["kept"]:
            print("  nothing survived the screen. That is the honest result for this data and")
            print(f"  a {args.horizon}-bar horizon against 10 bps a side, not a bug.")
        if data["dropped"]:
            print("  first drop reasons:")
            for drop in data["dropped"][:3]:
                print(f"    {drop['name']}: {drop['reason']}")
        ladder = data["ladder"]
        print(f"  ladder ({ladder['windows']} purged windows): "
              + ", ".join(
                  f"{row['level']} {row['net_expectancy_bps']:+.2f}bps" for row in ladder["levels"]
              ))
        print(f"  promoted: {ladder['promoted']}")
        leakage = data["leakage"]
        print(f"  leakage: {leakage['shuffled_labels']['verdict']}")
        if leakage["time_shift"].get("available"):
            shift = leakage["time_shift"]
            print(f"  time shift: IC {shift['ic_now']:+.3f} now, {shift['ic_near_shift']:+.3f} at +50 bars, "
                  f"{shift['ic_far_shift']:+.3f} at +{shift['far_shift_bars']} bars "
                  f"({'decays as expected' if shift['decays'] else 'DOES NOT DECAY - check for leakage'})")
        if data["importance"]:
            top = data["importance"][0]
            print(f"  biggest weight: {top['feature']} ({top['share']:.0%} of total)"
                  + (f" - {top['warning']}" if top.get("warning") else ""))
    return 0


async def _run_slippage(cfg: Config, days: int, seed: int | None) -> dict[str, Any]:
    """Run a session, then report what fills actually cost against what they expected."""
    from .core.clock import SimClock
    from .core.store import Store
    from .engine import Engine

    start = _sim_start(cfg)
    clock = SimClock(start)
    store = Store(":memory:")
    engine = await Engine.build(cfg, clock=clock, store=store)
    if seed is not None and hasattr(engine.feed, "rng"):
        engine.feed.rng.seed(seed)  # a replay has no rng: its prices are already fixed
    await engine.run(stop_at=_sim_end(cfg, start, days))
    recorder = engine.slippage
    summary = recorder.summary()
    model = recorder.model()
    calibration = recorder.calibration(model)
    rows = recorder.as_rows()
    engine.close()
    store.close()
    return {
        "days": days,
        "seed": seed,
        "summary": summary,
        "calibration": calibration,
        "model": model.table(),
        "samples": rows[:20],
        "assumed_bps": float(cfg.settings.paper.taker_fee) * 10_000,
    }


def cmd_slippage(args: argparse.Namespace) -> int:
    """Measure slippage instead of assuming it."""
    cfg = _config(args)
    report = asyncio.run(_run_slippage(cfg, args.days, args.seed))
    if args.json:
        _print(report)
        return 0

    summary = report["summary"]
    if not summary.get("fills"):
        print("no fills with an expected price were recorded - nothing to calibrate")
        return 0
    print(f"{summary['fills']} fills with an expected price "
          f"({summary['skipped']} without one could not be measured)")
    print(f"  median {summary['median_bps']:+.2f} bps | mean {summary['mean_bps']:+.2f} bps "
          f"| p95 {summary['p95_bps']:+.2f} bps | worst {summary['worst_bps']:+.2f} bps")
    print(f"  adverse on {summary['adverse_share'] * 100:.0f}% of fills "
          f"(positive = worse than the expected price)")
    print()
    print("by symbol:")
    for symbol, row in summary["by_symbol"].items():
        print(f"  {symbol:10s} {row['fills']:4d} fills | median {row['median_bps']:+.2f} bps "
              f"| p95 {row['p95_bps']:+.2f} bps")
    print()
    print("calibrated model (size vs visible depth -> slippage):")
    for row in report["model"][:12]:
        print(f"  {row['symbol']:10s} up to {row['size_vs_depth_up_to']:>4.2f}x depth | "
              f"n={row['samples']:3d} | median {row['median_bps']:+.2f} | p95 {row['p95_bps']:+.2f} | "
              f"worst {row['worst_bps']:+.2f} bps")
    calibration = report["calibration"]
    print()
    print(f"calibration: mean error {calibration['mean_error_bps']:+.2f} bps "
          f"(positive = model too pessimistic)")
    print(f"  {calibration['verdict']}")
    print(f"  assumed taker cost: {report['assumed_bps']:.1f} bps")
    return 0


def _save_promoted_model(
    frames: dict[str, Any], *, horizon: int, path: str
) -> dict[str, Any]:
    """Fit a ridge model and register it - only if the ladder actually promoted one.

    Registering a model is how it becomes able to trade, so this refuses unless the
    promotion rule was met on the same data and the net expectancy is positive.
    """
    from .ai.ladder import RidgeModel, run_ladder
    from .ai.registry import ModelRefusal, save_card

    symbol, frame = next(iter(frames.items()))
    ladder = run_ladder(frame, horizon=horizon, n_folds=3)
    best = next(level for level in ladder.levels if level.level == ladder.promoted)
    if ladder.promoted == "baseline" or best.net_expectancy <= 0:
        return {
            "saved": False,
            "reason": (
                f"nothing was promoted: {ladder.verdict}. A model card is not written for a "
                "model that has not beaten the simpler rung net of costs."
            ),
        }

    from .ai.features import forward_returns
    from .ai.ladder import purged_folds

    target = forward_returns(frame.close, horizon)
    folds = purged_folds(len(frame), n_folds=3, horizon=horizon)
    model = RidgeModel(alpha=10.0, names=frame.names).fit(frame, folds[0].train, target)
    try:
        card = save_card(
            path,
            name=f"ridge_{symbol.replace('/', '-')}",
            level=ladder.promoted,
            features=list(model.names or []),
            weights=[float(w) for w in (model.coef_[1:] if model.coef_ is not None else [])],
            intercept=float(model.coef_[0]) if model.coef_ is not None else 0.0,
            metrics={
                "net_expectancy": best.net_expectancy,
                "ic": best.ic,
                "hit_rate": best.hit_rate,
            },
            training_window={
                "symbol": symbol,
                "bars": len(frame),
                "horizon": horizon,
                "folds": ladder.windows,
            },
            notes=ladder.verdict,
        )
    except ModelRefusal as exc:
        return {"saved": False, "reason": str(exc)}
    return {"saved": True, "path": str(path), "card": card.as_dict()}


async def _run_meta(cfg: Config, days: int, seed: int | None, horizon: int, max_bars: int) -> dict[str, Any]:
    """Candidates from the screened signals, then the meta-model that would gate them."""
    from .ai.meta import evaluate_meta
    from .ai.signals import evaluate, screen

    _unused, frames, meta = await _collect_frames(cfg, days, seed)
    import numpy as np

    results: dict[str, Any] = {}
    for symbol, frame in frames.items():
        kept = screen(frame, primary_horizon=horizon).kept
        if not kept:
            results[symbol] = {"kept": 0, "reason": "no signals survived the screen"}
            continue
        # a candidate is any bar where the surviving signals agree on direction
        votes = np.zeros(len(frame))
        for spec in kept:
            evaluation = evaluate(frame, spec, primary_horizon=horizon)
            votes += np.sign(np.nan_to_num(spec.compute(frame))) * abs(evaluation.ic)
        candidates = votes > 0
        report, model, threshold = evaluate_meta(
            frame, candidates=candidates, max_bars=max_bars, n_folds=3
        )
        results[symbol] = {
            "kept_signals": [spec.name for spec in kept],
            "candidates": int(candidates.sum()),
            "report": report.as_dict(),
            "model_ready": model.ready,
            "threshold": threshold,
        }
    return {"days": days, "seed": seed, "horizon": horizon, "max_bars": max_bars,
            "meta": meta, "symbols": results}


def cmd_meta(args: argparse.Namespace) -> int:
    """Meta-labelling: would a second model improve the trades the rules propose?"""
    cfg = _config(args)
    report = asyncio.run(_run_meta(cfg, args.days, args.seed, args.horizon, args.max_bars))
    if args.json:
        _print(report)
        return 0

    for symbol, data in report["symbols"].items():
        print(f"{symbol}:")
        if not data.get("report"):
            print(f"  {data.get('reason', 'no data')}")
            continue
        row = data["report"]
        print(f"  {data['candidates']} candidate setups from {len(data['kept_signals'])} signals")
        print(f"  base win rate {row['base_rate']:.1%} | AUC {row['auc']:.3f} "
              f"| folds {row['folds']}")
        print(f"  gate at p>={row['threshold']:.2f}: takes {row['trades_taken']}, "
              f"skips {row['trades_skipped']}")
        print(f"  expectancy per trade: all {row['expectancy_all'] * 10_000:+.1f} bps | "
              f"taken {row['expectancy_taken'] * 10_000:+.1f} bps | "
              f"skipped {row['expectancy_skipped'] * 10_000:+.1f} bps")
        print(f"  verdict: {row['verdict']}")
    return 0


def cmd_robustness(args: argparse.Namespace) -> int:
    cfg = _config(args)
    report = asyncio.run(_run_robustness(cfg, args.days, args.seed, args.cost_multiplier))
    if args.json:
        _print(report)
        return 0

    nominal, stressed = report["nominal"], report["stressed"]
    print(f"nominal:  return {nominal['return_pct']:+.2f}% | PF {nominal['profit_factor']:.2f} "
          f"| sharpe {nominal['sharpe']:.2f} | {nominal['trades']} trades")
    print(f"{args.cost_multiplier:g}x costs: return {stressed['return_pct']:+.2f}% | PF {stressed['profit_factor']:.2f} "
          f"| sharpe {stressed['sharpe']:.2f} | {stressed['trades']} trades")
    print()
    for name, check in report["checks"].items():
        print(f"{'PASS' if check['ok'] else 'FAIL'}  {name:24s} {check['detail']}")
    bench = report.get("benchmark")
    if bench:
        comparison = report["benchmark_comparison"]
        print(f"\nbenchmark (buy and hold over the retained bars, fees included): "
              f"{bench['return_pct']:+.2f}%")
        print(f"  {comparison['verdict']}")
    attribution = report["attribution"]
    print(f"\nattribution: net {attribution['net_pnl']:+.2f} after {attribution['fees']:.2f} fees "
          f"({attribution['fee_drag_pct_of_gross']:.0f}% of gross)")
    for label, key in (("by strategy", "by_strategy"), ("by symbol", "by_symbol")):
        for name, row in attribution[key].items():
            print(f"  {label:12s} {name:12s} pnl {row['pnl']:+9.2f} | {row['trades']:3d} trades "
                  f"| win {row['win_rate'] * 100:4.1f}%")
    if attribution.get("depends_on_one_trade"):
        print(f"  warning: profit disappears without the single best trade "
              f"({attribution['pnl_without_best_trade']:+.2f})")
    print(f"\nrobustness: {report['verdict']}")
    print("promotion gates:", "PASS" if report["passed"] else "NOT PROMOTABLE (see `tradebot metrics`)")
    return 0


def _start_command_listener(cfg: Config, engine: Any) -> asyncio.Task[Any] | None:
    """Start the inbound command listener when a token and a chat id are configured."""
    from .alerts.commands import CommandRouter, build_ops
    from .alerts.telegram import serve, transport_from_config

    transport = transport_from_config(cfg)
    chat = str(cfg.secrets.telegram_chat_id or "").strip()
    if transport is None or not chat:
        return None
    allowed: frozenset[int] | frozenset[str]
    if chat.lstrip("-").isdigit():
        allowed = frozenset({int(chat)})
    elif chat:
        allowed = frozenset({chat})
    else:  # pragma: no cover - guarded by the caller
        return None
    def audit(kind: str, payload: dict[str, Any]) -> None:
        engine.store.log_event(kind, payload, actor="telegram")

    router = CommandRouter(
        ops=build_ops(engine=engine, store=engine.store, clock=engine.clock),
        whitelist=allowed,
        log=audit,
    )
    return asyncio.create_task(serve(router, transport))


def cmd_telegram(args: argparse.Namespace) -> int:
    """Check the inbound Telegram wiring without pretending to run a bot.

    The listener needs a running engine to answer ``/status`` or ``/flatten``, so it is
    started by ``run``. On its own this command verifies the token, the whitelist and that
    Telegram answers at all - the three things that are wrong when commands go silent.
    """
    from .alerts.commands import HELP
    from .alerts.telegram import transport_from_config

    cfg = _config(args)
    transport = transport_from_config(cfg)
    chat = str(cfg.secrets.telegram_chat_id or "").strip()
    report: dict[str, Any] = {
        "token_configured": transport is not None,
        "whitelist": [chat] if chat else [],
        "listener_starts_with": "tradebot run",
    }
    if transport is None:
        report["verdict"] = "no bot token: the listener will not start"
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(report["verdict"])
            print("  set TELEGRAM_BOT_TOKEN (and TELEGRAM_CHAT_ID) to enable it")
            print(f"  the listener would answer these commands:\n\n{HELP}")
        return 2
    if not chat:
        report["verdict"] = "token present but no allowed chat id: every command would be refused"
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(report["verdict"])
        return 2

    async def _probe() -> dict[str, Any]:
        updates = await transport.get_updates(timeout=args.poll_timeout)
        return {
            "telegram_reachable": transport.errors == 0,
            "pending_updates": len(updates),
            "http_calls": transport.calls,
        }

    report.update(asyncio.run(_probe()))
    report["verdict"] = (
        "token and chat id look right; `tradebot run` will answer commands"
        if report.get("telegram_reachable")
        else "telegram refused or did not answer - check the token"
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"token: ok   chat id: {chat}   telegram reachable: {report.get('telegram_reachable')}")
        print(f"  pending updates: {report.get('pending_updates')} (offset starts at 0, so a "
              "backlog is normal on the first check)")
        print(f"  verdict: {report['verdict']}")
    return 0 if report.get("telegram_reachable") else 3


def cmd_backfill(args: argparse.Namespace) -> int:
    """Paginate historical candles into the Parquet archive and report the coverage.

    `--source demo` runs against a deterministic offline exchange so the pagination,
    de-duplication and gap detection can be seen working; `--source ccxt` is the real
    thing and needs exchange access this sandbox does not have.
    """
    from .data.archive import Archive
    from .data.backfill import Backfill, rest_fetcher, synthetic_fetcher

    cfg = _config(args)
    symbols = [s.strip() for s in (args.symbols or ",".join(cfg.symbol_names())).split(",") if s.strip()]
    end = parse_iso(args.to_dt) if args.to_dt else utcnow()
    start = parse_iso(args.from_dt) if args.from_dt else (end - timedelta(days=max(1, args.days)))
    fetcher = synthetic_fetcher(seed=args.seed, page_size=args.page_limit) if args.source == "demo" else rest_fetcher(cfg)
    backfill = Backfill(fetcher, page_limit=args.page_limit, max_pages=args.max_pages)
    root = args.root or cfg.settings.archive_dir or (cfg.settings.data_dir / "archive")
    archive = Archive(str(root)) if args.write else None

    async def _main() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for symbol in symbols:
            candles, report = await backfill.run(symbol, args.timeframe, start, end)
            entry = report.as_dict()
            if archive is not None and candles:
                paths = archive.write_candles(candles)
                entry["archived_files"] = [str(path) for path in paths]
            out.append(entry)
        closer = getattr(fetcher, "aclose", None)
        if closer is not None:
            await closer()  # a live ccxt session left open leaks a socket and logs loudly
        return out

    reports = asyncio.run(_main())
    ok = all(report["kept"] > 0 and report["error"] is None for report in reports)
    if args.json:
        print(json.dumps({"source": args.source, "reports": reports}, indent=2, default=str))
    else:
        print(f"backfill {start.isoformat(timespec='minutes')} -> {end.isoformat(timespec='minutes')} ({args.timeframe})")
        for report in reports:
            verdict = "complete" if report["complete"] else ("STUCK" if report["stuck"] else "incomplete")
            print(
                f"  {report['symbol']:10s} pages {report['pages']:3d}  kept {report['kept']:5d}"
                f"  expected {report['expected_bars']:5d}  coverage {report['coverage'] * 100:5.1f}%"
                f"  gaps {len(report['gaps']):2d}  duplicates {report['duplicates']:3d}  {verdict}"
            )
            if report["error"]:
                print(f"             error: {report['error']}")
            if report.get("archived_files"):
                print(f"             archived {len(report['archived_files'])} partition(s)")
        if args.source == "demo":
            print("  (demo: a deterministic offline exchange - the pagination is real, the prices are not)")
        elif not ok:
            print("  the real exchange refused or was unreachable; nothing was invented to fill the gap")
    return 0 if ok else 4


def _month_chunks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Split a window on UTC month boundaries, so each chunk is one resume unit."""
    out: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        year, month = cursor.year, cursor.month
        next_month = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=UTC)
        stop = min(next_month, end)
        out.append((cursor, stop))
        cursor = stop
    return out


def cmd_backfill_history(args: argparse.Namespace) -> int:
    """Record months of history in one go, and be honest about what did not land.

    A six-month backfill is six chances to lose a page and one long wait to find out.
    This splits the window on month boundaries, writes each chunk to the archive as it
    arrives, and keeps a resume file so a failed night costs one chunk rather than all of
    them. It never fills a hole with invented bars: an incomplete chunk stays incomplete
    in the state file, is printed as such, and makes the command exit non-zero.
    """
    from .data.archive import Archive
    from .data.backfill import Backfill, rest_fetcher, synthetic_fetcher

    cfg = _config(args)
    symbols = [s.strip() for s in (args.symbols or ",".join(cfg.symbol_names())).split(",") if s.strip()]
    end = parse_iso(args.to_dt) if args.to_dt else utcnow()
    if args.from_dt:
        start = parse_iso(args.from_dt)
    else:
        if args.months <= 0:
            print("error: --months must be positive", file=sys.stderr)
            return 2
        year, month = divmod(end.year * 12 + end.month - 1 - args.months, 12)
        if year < 1:
            print("error: --months exceeds the supported calendar", file=sys.stderr)
            return 2
        start = datetime(year, month + 1, 1, tzinfo=UTC)
    if end <= start:
        print("error: the window must end after it starts", file=sys.stderr)
        return 2
    root = args.root or cfg.settings.archive_dir or (cfg.settings.data_dir / "archive")
    state_path = Path(args.state) if args.state else cfg.settings.data_dir / "backfill-history.json"
    state: dict[str, Any] = {}
    if state_path.exists():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            state = json.loads(state_path.read_text() or "{}")
    chunks = state.setdefault("chunks", {})
    fetcher = (
        synthetic_fetcher(seed=args.seed, page_size=args.page_limit)
        if args.source == "demo"
        else rest_fetcher(cfg)
    )
    backfill = Backfill(fetcher, page_limit=args.page_limit, max_pages=args.max_pages)
    archive = Archive(str(root))
    plan = _month_chunks(start, end)

    async def _main() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            for chunk_start, chunk_end in plan:
                for symbol in symbols:
                    key = f"{symbol}|{args.timeframe}|{chunk_start.date()}|{chunk_end.date()}"
                    done = chunks.get(key)
                    if done and done.get("complete") and not args.retry_incomplete:
                        rows.append({**done, "skipped": True})
                        continue
                    candles, report = await backfill.run(symbol, args.timeframe, chunk_start, chunk_end)
                    entry = report.as_dict()
                    entry["chunk"] = [chunk_start.isoformat(), chunk_end.isoformat()]
                    entry["skipped"] = False
                    if candles:
                        entry["archived_files"] = [str(path) for path in archive.write_candles(candles)]
                    chunks[key] = {k: v for k, v in entry.items() if k != "archived_files"}
                    rows.append(entry)
                    state["updated_at"] = utcnow().isoformat()
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    state_path.write_text(json.dumps(state, indent=2, default=str))
        finally:
            closer = getattr(fetcher, "aclose", None)
            if closer is not None:
                await closer()
        return rows

    rows = asyncio.run(_main())
    bad = [row for row in rows if row.get("error") or not row.get("kept") or not row.get("complete")]
    summary = {
        "source": args.source,
        "window": [start.isoformat(), end.isoformat()],
        "symbols": symbols,
        "chunks": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in plan],
        "archive_root": str(root),
        "state_file": str(state_path),
        "rows": rows,
        "kept_total": sum(int(row.get("kept") or 0) for row in rows),
        "gap_total": sum(len(row.get("gaps") or []) for row in rows),
        "incomplete": [row["symbol"] for row in bad],
        "complete": not bad,
    }
    if args.json:
        _print(summary)
        return 0 if not bad else 4

    print(
        f"history backfill: {len(plan)} chunk(s) x {len(symbols)} symbol(s), "
        f"{start.date()} .. {end.date()} ({args.timeframe})"
    )
    if args.source == "demo":
        print("  (demo: the pagination is real, the prices are not)")
    for row in rows:
        verdict = "skipped" if row.get("skipped") else (
            "complete" if row.get("complete") else ("STUCK" if row.get("stuck") else "incomplete")
        )
        chunk = row.get("chunk") or ["?", "?"]
        print(
            f"  {str(chunk[0])[:10]}  {row['symbol']:10s} pages {row.get('pages', 0):4d}"
            f"  kept {int(row.get('kept') or 0):6d}  expected {int(row.get('expected_bars') or 0):6d}"
            f"  coverage {float(row.get('coverage') or 0) * 100:5.1f}%"
            f"  gaps {len(row.get('gaps') or []):2d}  {verdict}"
        )
    stats = archive.coverage()["candles"]
    print()
    print(
        f"archive: {stats['files']} file(s) | {len(stats['symbols'])} symbol(s) | "
        f"{stats['days']} day(s) ({stats['first_day']} .. {stats['last_day']}) | "
        f"{stats['bytes'] / 1024 / 1024:.1f} MiB"
    )
    print(f"resume file: {state_path} (rerun the same command to continue)")
    if bad:
        print(f"incomplete: {len(bad)} chunk(s) - rerun, or add --retry-incomplete")
        return 4
    print(f"all {len(plan) * len(symbols)} chunk(s) complete | {summary['kept_total']:,} bars | gaps {summary['gap_total']}")
    return 0



def cmd_learn(args: argparse.Namespace) -> int:
    """Learn from the bot's own closed trades: label, purge, walk forward, decide."""
    from .ai.ledger import (
        MIN_TRADES,
        fit_gate,
        judge,
        save_card,
        trades_from_store,
        walk_forward,
    )

    cfg = _config(args)
    if getattr(args, "inspect", None):
        from .ai.ledger import load_card

        guards = getattr(cfg.settings, "live_guards", None)
        max_age = float(getattr(guards, "go_nogo_max_age_days", 14) or 0)
        try:
            card = load_card(args.inspect, max_age_days=max_age)
        except (OSError, ValueError) as exc:
            print(f"cannot read that card: {exc}", file=sys.stderr)
            return 2
        verdict = card.meta.get("verdict") or {}
        summary = verdict.get("summary") or {}
        age = card.age_days()
        print(f"card: {args.inspect}")
        print(f"  created {card.meta.get('created_at')} ({'age unknown' if age is None else f'{age:.1f} days old'})")
        print(f"  threshold {card.threshold:.3f}, {len(card.vocabulary.strategies)} strategies x {len(card.vocabulary.symbols)} symbols")
        print(f"  trained on {summary.get('trades', '?')} trades, verdict {verdict.get('status', '?')}")
        if card.stale_reason:
            print(f"  INERT: {card.stale_reason}")
            return 3
        print("  valid: it will veto entries the ledger lost on")
        return 0

    db_path = args.db or cfg.settings.db_path
    if not Path(db_path).exists():
        print(f"no database at {db_path}: run the bot, or backtest with --persist", file=sys.stderr)
        return 1
    store = Store(db_path)
    try:
        report = trades_from_store(store, limit=100_000)
    except Exception:
        store.close()
        raise

    windows = walk_forward(report.trades, windows=args.windows)
    verdict = judge(report, windows, min_trades=args.min_trades)
    summary = report.summary()
    card_path = None
    if args.save and verdict.may_write_card:
        gate = fit_gate(report.trades)
        card_path = str(save_card(args.save, gate, verdict, report=report))
    # every attempt is a trial: log it, so a later multiple-testing report counts it
    run_id = f"ledger-{utcnow().strftime('%Y%m%dT%H%M%S')}"
    try:
        store.save_experiment(
            run_id,
            kind="ledger_gate",
            name=f"ledger walk-forward ({len(windows)} windows)",
            strategy="ledger",
            params={"windows": args.windows, "min_trades": args.min_trades},
            metrics={
                "trades": summary.get("trades", 0),
                "win_rate": summary.get("win_rate", 0.0),
                "mean_r": summary.get("mean_r", 0.0),
                "fee_share_of_gross": summary.get("fee_share_of_gross", 0.0),
                "windows": len(windows),
                "auc_min": min((window.auc for window in windows), default=0.0),
                "gated_r_min": min((window.gated_r for window in windows), default=0.0),
            },
            artifacts={"card": card_path or ""},
            notes=verdict.status,
        )
    except Exception as exc:  # noqa: BLE001 - a log write must not lose the verdict
        print(f"warning: could not log this attempt: {type(exc).__name__}", file=sys.stderr)
    finally:
        store.close()

    if args.json:
        _print(
            {
                "ledger": summary,
                "windows": [window.as_dict() for window in windows],
                "verdict": verdict.as_dict(),
                "card": card_path,
                "run_id": run_id,
            }
        )
        return 0

    if not report.trades:
        print(f"no closed trades in {db_path}: run the bot first")
        return 1
    print(f"ledger: {summary['trades']} closed trades, {summary['first'][:10]} -> {summary['last'][:10]}")
    if summary.get("skipped"):
        reasons = ", ".join(f"{count} {reason}" for reason, count in summary["reasons"].items())
        print(f"  skipped {summary['skipped']} ({reasons})")
    print(
        f"  win rate {summary['win_rate']:.1%}, mean {summary['mean_r']:+.2f}R, "
        f"median {summary['median_r']:+.2f}R, total {summary['total_r']:+.2f}R, "
        f"fees ate {summary['fee_share_of_gross']:.0%} of gross"
    )
    print()
    if windows:
        below = len(report.trades) < args.min_trades
        print(
            f"purged walk-forward over the ledger, {len(windows)} windows"
            + (" (indicative only: below the sample floor)" if below else "")
        )
        print(f"  {'window':22s} {'train':>6s} {'test':>5s} {'AUC':>6s} {'all':>7s} {'gated':>7s} {'2x':>7s} {'lift':>7s}")
        for window in windows:
            span = f"{window.start:%m-%d}->{window.end:%m-%d}"
            print(
                f"  {span:22s} {window.n_train:6d} {window.n_test:5d} {window.auc:6.3f} "
                f"{window.base_r:+7.3f} {window.gated_r:+7.3f} {window.stress_r:+7.3f} "
                f"{window.improvement:+6.0%}"
            )
        print()
    else:
        print("purged walk-forward: not enough resolved trades to build a window")
        print(f"  a window needs {40} training trades closed before it and {20} inside it")
        print()
    print("\n".join(verdict.lines()))
    print(f"  logged as experiment {run_id} (kind ledger_gate)")
    print()
    if verdict.status == "PROMOTABLE":
        print(
            "the gate beat taking every trade out of sample and survived 2x costs."
            if not card_path
            else f"card written to {card_path}; install it with `tradebot run --ledger-gate {card_path}`"
        )
    else:
        print(
            f"nothing to install. {MIN_TRADES} trades is the same floor the acceptance gates use;\n"
            "until then the rule trades unfiltered, which is the honest default."
        )
    return 0


def cmd_experiments(args: argparse.Namespace) -> int:
    """Show the experiment log, export it, or read a file store back."""
    from .ai.tracking import export_from_store, read_runs

    if getattr(args, "read_server", None):
        from .ai.tracking import read_server_runs

        try:
            runs = read_server_runs(args.read_server, limit=args.limit)
        except Exception as exc:  # noqa: BLE001 - a wrong URI is a message, not a traceback
            print(f"cannot read {args.read_server}: {exc.__class__.__name__}: {exc}")
            return 1
        if args.json:
            _print({"server": args.read_server, "runs": runs})
            return 0
        if not runs:
            print(f"no runs at {args.read_server}: is anything logging there?")
            return 0
        print(f"{len(runs)} run(s) from {args.read_server}")
        for run in runs:
            head = run["metrics"].get("equity") or run["metrics"].get("end_equity")
            points = ", ".join(f"{k}={v:.4g}" for k, v in list(run["metrics"].items())[:4])
            print(
                f"  {run['status']:10s} {run['run_id'][:28]:28s} "
                f"equity={head if head is not None else '-':>12}  {points}"
            )
        return 0

    if args.read:
        runs = read_runs(args.read, limit=args.limit)
        if args.json:
            _print({"store": args.read, "runs": runs})
            return 0
        if not runs:
            print(f"no runs in {args.read}: is that a directory of experiments?")
            return 0
        print(f"{len(runs)} run(s) in {args.read}")
        for run in runs:
            head = run["metrics"].get("equity") or run["metrics"].get("end_equity")
            summary = ", ".join(
                f"{key}={value:.4g}" for key, value in list(run["metrics"].items())[:4]
            )
            print(
                f"  {run['experiment']:20s} {run['run_id'][:28]:28s} "
                f"equity={head if head is not None else '-':>12}  {summary}"
            )
        kinds = sorted({run["tags"].get("run_kind") or run["tags"].get("kind") or "?" for run in runs})
        print(f"\nrun kinds: {', '.join(kinds)}")
        return 0

    cfg = _config(args)
    store = Store(cfg.settings.db_path)
    try:
        rows = store.experiments(limit=args.limit, strategy=args.strategy or None)
        report = None
        if args.export:
            report = export_from_store(store, args.export, limit=args.limit)
    finally:
        store.close()

    if args.json:
        print(
            json.dumps(
                {
                    "experiments": len(rows),
                    "export": report.as_dict() if report else None,
                    "runs": [
                        {
                            "run_id": row["run_id"],
                            "kind": row["kind"],
                            "strategy": row["strategy"],
                            "created_at": row["created_at"],
                            "metrics": row["metrics"],
                        }
                        for row in rows[:20]
                    ],
                },
                indent=2,
                default=str,
            )
        )
        return 0
    if not rows:
        print("no experiments logged yet: run a backtest, sensitivity grid or signal screen")
        return 0
    print(f"{len(rows)} experiment(s) in {cfg.settings.db_path}")
    for row in rows[:10]:
        metrics = ", ".join(f"{k}={v}" for k, v in list(row["metrics"].items())[:4])
        print(f"  {row['created_at'][:19]}  {row['kind']:12s} {row['strategy']:16s} {row['run_id'][:12]}  {metrics}")
    if report is not None:
        print(f"exported {report.runs} run(s): {report.metrics} metrics, {report.params} params, "
              f"{report.tags} tags, {report.runs_as_tags} non-numeric metrics moved to tags")
        print(f"  open with: {report.as_dict()['ui']}")
        if report.skipped:
            print(f"  skipped: {report.skipped}")
    return 0


def cmd_research(args: argparse.Namespace) -> int:
    """The bounded research loop: search a declared space, count every attempt, deflate."""
    from .research import format_report, search, space_from_config

    cfg = _config(args)
    strategy = args.strategy
    if args.space:
        space = _parse_axes(args.space, cfg, strategy)
    else:
        space = space_from_config(cfg, strategy, steps=args.steps, spread=args.spread)
    if not space:
        print(f"{strategy} has no numeric parameters to search", file=sys.stderr)
        return 2

    start = _sim_start(cfg)
    end = _sim_end(cfg, start, args.days)
    report = asyncio.run(
        search(
            cfg,
            strategy=strategy,
            space=space,
            start=start,
            end=end,
            stages=tuple(args.stage or (0.25, 0.5, 1.0)),
            keep=args.keep,
            budget=args.budget,
            seed=args.seed,
        )
    )
    from .backtest.robustness import log_trials
    from .core.store import Store

    store = Store(cfg.settings.db_path)
    try:
        log_trials(
            store,
            strategy,
            [trial.as_dict() for trial in report.trials],
            metric="net_return",
            seed=args.seed,
            run_prefix="research",
        )
    finally:
        store.close()

    if args.json:
        _print(report.as_dict())
    else:
        print(format_report(report))
    return 0 if report.recommend or args.json else 4


def cmd_news(args: argparse.Namespace) -> int:
    """Fetch the configured feed once and show what the sentiment overlay would do.

    Read-only, and deliberately dull: a lexicon score per headline and the size
    multiplier it implies, so the number that reaches the engine can be inspected before
    anyone trusts it.
    """
    from .ai.sentiment import LexiconScorer, explain
    from .news import NewsPoller

    cfg = _config(args)
    urls = [url.strip() for url in str(args.url).split(",") if url.strip()] if args.url else []
    if not urls and not args.file:
        print("no feed: pass --url https://example.com/rss or --file feed.xml", file=sys.stderr)
        return 2
    poller = NewsPoller(urls=tuple(urls) or ("file://local",), clock=Clock())
    if args.file:
        from pathlib import Path as _Path

        from .ai.sentiment import parse_simple_rss

        text = _Path(args.file).read_text(encoding="utf-8", errors="replace")
        batch = parse_simple_rss(text, source=str(args.file))
        poller.stats.polls += 1
        poller.stats.fetched += 1
        poller.stats.parsed += len(batch)
        poller.stats.added += poller.store.add_many(batch)
        poller.stats.last_poll_at = poller.clock.now()
    if urls:
        asyncio.run(poller.poll_once())
    overlay = poller.overlay
    now = poller.clock.now()
    symbols: list[str] = args.symbols.split(",") if args.symbols else cfg.symbol_names()
    report: dict[str, Any] = {
        "feeds": urls,
        "stats": poller.stats.as_dict(),
        "symbols": explain(overlay, {symbol: symbol for symbol in symbols}, now),
        "headlines": [
            {"text": headline.text, "ts": headline.ts.isoformat(), "score": LexiconScorer().score(headline.text)}
            for headline in list(poller.store._items)[-args.limit :]
        ],
    }
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0 if poller.stats.fetched else 3
    if not poller.stats.fetched:
        print(f"could not read any feed (last error: {poller.stats.last_error})", file=sys.stderr)
        return 3
    sources = len(urls) or (1 if args.file else 0)
    print(
        f"polled {sources} feed(s): {poller.stats.parsed} headline(s), "
        f"{poller.stats.added} new, {poller.stats.duplicates} already seen"
    )
    rows: list[dict[str, Any]] = report["headlines"]
    decisions: dict[str, dict[str, Any]] = report["symbols"]
    for headline in rows:
        print(f"  {headline['score']:+.2f}  {headline['ts'][:16]}  {headline['text'][:96]}")
    print("\nwhat this would do to entry size:")
    for symbol, decision in decisions.items():
        print(
            f"  {symbol:10s} x{decision['multiplier']:.2f}  score {decision['score']:+.2f}  {decision['reason']}"
        )
    print("\n(the multiplier is capped at 1.0: good news never makes an order bigger)")
    return 0


def _frame_ts(frame: Any) -> datetime | None:
    """The instant an exchange frame refers to, if it says."""
    data = frame.get("data") if isinstance(frame, dict) else None
    if not isinstance(data, dict):
        return None
    raw = data.get("E") or (data.get("k") or {}).get("t")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def cmd_stream(args: argparse.Namespace) -> int:
    """Replay a recorded session through the streaming feed (or try the live socket).

    The replay path is the point: it puts real frames - klines, book snapshots, trades,
    one piece of garbage, one dropped connection - through the exact code that would run
    against Binance, and reports the feed's health afterwards. Nothing here needs a
    network, and nothing here is a mock of the feed.
    """
    from .core.clock import UTC, SimClock
    from .data.feed import SyntheticFeed
    from .data.stream import WebSocketFeed, summarise_events

    cfg = _config(args)
    if args.live:
        return _run_live_stream(cfg, args)

    start = datetime(2024, 1, 1, tzinfo=UTC)
    clock = SimClock(start)
    source = SyntheticFeed(cfg, clock)
    source.rng.seed(args.seed)
    symbols = cfg.symbol_names()
    target = max(1, args.minutes) * max(1, len(symbols))
    bars: list[Any] = []
    books: list[Any] = []

    async def _collect() -> None:
        async def emit(kind: str, payload: Any) -> None:
            if kind == "bar":
                bars.append(payload)
            elif kind == "book":
                books.append(payload)
            # The unpaced synthetic feed never suspends on its own, so without this the
            # collector below never gets a turn and the command hangs (it did).
            await asyncio.sleep(0)

        task = asyncio.create_task(source.run(emit))
        for _ in range(200_000):
            if len(bars) >= target:
                break
            await asyncio.sleep(0)
        source.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_collect())
    if not bars:
        print("error: the synthetic source produced no bars", file=sys.stderr)
        return 2

    # --- turn the recording into exchange-shaped frames -------------------
    exchange_symbol = lambda symbol: str(symbol).replace("/", "")  # noqa: E731
    frames: list[Any] = []
    for index, bar in enumerate(bars):
        frames.append(
            {
                "stream": f"{exchange_symbol(bar.symbol).lower()}@kline_1m",
                "data": {
                    "e": "kline",
                    "s": exchange_symbol(bar.symbol),
                    "k": {
                        "t": int(bar.ts.timestamp() * 1000),
                        "T": int(bar.close_time.timestamp() * 1000) - 1,
                        "s": exchange_symbol(bar.symbol),
                        "i": bar.timeframe,
                        "o": str(bar.open),
                        "c": str(bar.close),
                        "h": str(bar.high),
                        "l": str(bar.low),
                        "v": str(bar.volume),
                        "x": True,
                    },
                },
            }
        )
        if index % 3 == 0:
            frames.append(
                {
                    "data": {
                        "e": "trade",
                        "s": exchange_symbol(bar.symbol),
                        "p": str(bar.close),
                        "q": str(bar.volume / 2),
                        "T": int(bar.close_time.timestamp() * 1000),
                        "m": index % 2 == 0,
                    }
                }
            )
        if books and index % 10 == 0:
            book = books[min(len(books) - 1, index)]
            frames.append(
                {
                    "data": {
                        "e": "depth20",
                        "s": exchange_symbol(bar.symbol),
                        "E": int(book.ts.timestamp() * 1000),
                        "bids": [[str(lvl.price), str(lvl.qty)] for lvl in book.bids[:5]],
                        "asks": [[str(lvl.price), str(lvl.qty)] for lvl in book.asks[:5]],
                    }
                }
            )
        if index and index % 97 == 0:
            frames.append("{not json at all")  # the exchange will do this eventually

    # One dropped connection in the middle, with the missed bars removed so the gap
    # filler has something real to supply.
    cut = len(frames) // 2
    dropped = list(frames[cut : cut + 6])
    first, second = frames[:cut], frames[cut + 6 :]

    by_key = {(bar.symbol, bar.ts): bar for bar in bars}

    async def gap_filler(symbol: str, since: datetime, until: datetime) -> list[Any]:
        return [
            bar
            for (sym, ts), bar in sorted(by_key.items(), key=lambda item: item[0][1])
            if sym == symbol and since <= ts < until
        ]

    #: Wall-clock instant of the first frame after the outage, so the replay can advance
    #: its clock across the downtime the way real time would have.
    resume_at = _frame_ts(second[0]) if second else None
    attempts = {"n": 0}

    async def transport(streams: Sequence[str]) -> Any:
        attempts["n"] += 1
        if attempts["n"] == 2 and resume_at is not None:
            ahead = (resume_at - clock.now()).total_seconds()
            if ahead > 0:
                clock.advance(ahead)
        chunk = first if attempts["n"] == 1 else second if attempts["n"] == 2 else []
        for frame in chunk:
            yield frame

    feed = WebSocketFeed(
        cfg,
        clock,
        transport=transport,
        gap_filler=gap_filler,
        max_backoff_seconds=0.0,
        max_reconnects=1,
    )
    events: list[tuple[str, Any]] = []

    async def emit(kind: str, payload: Any) -> None:
        events.append((kind, payload))

    asyncio.run(feed.run(emit))
    stats = summarise_events(events)
    health = feed.describe()
    unknown = sorted({str(p.symbol) for _, p in events if str(getattr(p, "symbol", "")) not in symbols})
    missing = [symbol for symbol in symbols if symbol not in stats.symbols]

    report: dict[str, Any] = {
        "mode": "replay",
        "source": "synthetic recording",
        "recorded_bars": len(bars),
        "recorded_books": len(books),
        "frames_sent": len(frames),
        "frames_dropped_with_the_connection": len(dropped),
        "attempts": attempts["n"],
        "events": stats.as_dict(),
        "health": health,
        "symbols_missing_from_the_stream": missing,
        "symbols_the_engine_would_not_recognise": unknown,
        "gap_fill_worked": health["health"]["gaps_filled"] > 0,
        "drop_detected": health["health"]["connects"] > 1,
        "decoder_rejected_junk": health["health"]["decode_errors"] > 0,
        "verdict": "",
    }
    ok = not missing and not unknown and report["gap_fill_worked"] and report["decoder_rejected_junk"]
    report["verdict"] = (
        "stream contract holds: every bar arrived under the engine's symbol, the dropped "
        "connection was noticed and the missed bars backfilled"
        if ok
        else "stream contract FAILED - see the missing/unknown symbols above"
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"replayed {len(bars)} bars and {len(books)} books as {len(frames)} exchange frames")
        print(
            f"  events   bars {stats.bars:4d}  books {stats.books:3d}  ticks {stats.ticks:4d}"
            f"   decode errors {health['health']['decode_errors']}"
        )
        print(
            f"  connection attempts {attempts['n']}  connects {health['health']['connects']}"
            f"  error reconnects {health['health']['reconnects']}"
            f"  gap-filled bars {health['health']['gaps_filled']}"
        )
        print(f"  last error: {health['health']['last_error'] or 'none'}")
        print(f"  verdict: {report['verdict']}")
        print("  live mode (`--live`) needs `pip install websockets` and exchange access;")
        print("  this sandbox has neither, so the socket itself stays unexercised.")
    return 0 if ok else 4


def _run_live_stream(cfg: Any, args: argparse.Namespace) -> int:
    """Try the real socket for a few seconds and report exactly what happens."""
    from .data.stream import live_feed

    feed = live_feed(cfg)
    events: list[Any] = []

    async def emit(kind: str, payload: Any) -> None:
        events.append((kind, payload))

    async def _main() -> str:
        task = asyncio.create_task(feed.run(emit))
        try:
            await asyncio.wait_for(task, timeout=max(1.0, args.seconds))
            return "socket closed on its own"
        except TimeoutError:
            feed.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return f"ran for {args.seconds:.0f}s"
        except Exception as exc:  # noqa: BLE001 - this is a diagnostic command
            return f"{type(exc).__name__}: {exc}"

    outcome = asyncio.run(_main())
    health = feed.health
    report = {
        "mode": "live",
        "symbols": len(cfg.symbol_names()),
        "events": len(events),
        "connects": health.connects,
        "reconnects": health.reconnects,
        "last_error": health.last_error,
        "outcome": outcome,
    }
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"live stream: {outcome}")
        print(f"  events received: {len(events)}  connects {health.connects}  reconnects {health.reconnects}")
        print(f"  last socket error: {health.last_error or 'none'}")
        print("  a real socket needs network access; without it this reports the error rather than pretending")
    return 0 if events else 4


def cmd_archive(args: argparse.Namespace) -> int:
    """Record a session into the Parquet archive, then report what is there."""
    from .backtest.parity import _event_rows  # noqa: F401 - keep imports honest
    from .data.archive import Archive, write_tape_to_archive
    from .data.tape import RecordingFeed

    cfg = _config(args)
    root = args.root or (cfg.settings.data_dir / "archive")
    archive = Archive(root)
    written: dict[str, Any] = {}

    if args.record_minutes > 0:
        from .core.clock import SimClock
        from .core.store import Store
        from .data.feed import SyntheticFeed
        from .engine import Engine

        async def _record() -> object:
            start = datetime(2024, 1, 1, tzinfo=UTC)
            clock = SimClock(start)
            store = Store(":memory:")
            synthetic = SyntheticFeed(cfg, clock)
            synthetic.rng.seed(args.seed)
            feed = RecordingFeed(cfg, synthetic, clock)
            engine = await Engine.build(cfg, clock=clock, store=store, feed=feed)
            await engine.run(stop_at=start + timedelta(minutes=args.record_minutes))
            try:
                return write_tape_to_archive(archive, feed.tape.events)
            finally:
                engine.close()

        from .data.archive import ArchiveStats

        stats = asyncio.run(_record())
        written = stats.as_dict() if isinstance(stats, ArchiveStats) else {}

    removed = 0
    if args.keep_days is not None:
        removed = len(archive.apply_retention(keep_days=args.keep_days))

    report: dict[str, Any] = {
        "root": str(root),
        "written": written,
        "removed": removed,
        "coverage": archive.coverage(),
    }
    if args.json:
        _print(report)
        return 0
    if written:
        print(f"recorded: {written['candles']} candles, {written['books']} books, "
              f"{written['bytes'] / 1024:.0f} KiB")
    coverage: dict[str, Any] = dict(report["coverage"])
    for kind, info in coverage.items():
        print(
            f"{kind}: {info['files']} files | {len(info['symbols'])} symbols | "
            f"{info['days']} days ({info['first_day']} .. {info['last_day']}) | "
            f"{info['bytes'] / 1024:.0f} KiB"
        )
    if removed:
        print(f"retention removed {removed} book partitions")
    return 0


def cmd_venues(args: argparse.Namespace) -> int:
    """Show the venue seams: is it open, when must we flatten, what does a round trip cost."""
    from .venues import (
        AlwaysOpenCalendar,
        CryptoCostModel,
        Instrument,
        NSECalendar,
        NSECostModel,
        SquareOffScheduler,
        compare_models,
        round_trip,
    )

    cfg = _config(args)
    calendar = NSECalendar() if args.venue == "nse" else AlwaysOpenCalendar()
    at = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else datetime.now(UTC)
    scheduler = SquareOffScheduler(calendar)
    plan = scheduler.plan(at)
    fees = cfg.settings.paper
    crypto = CryptoCostModel(maker_fee=fees.maker_fee, taker_fee=fees.taker_fee)
    models = [crypto, NSECostModel()]
    notional = D(args.notional)

    report: dict[str, Any] = {
        "venue": calendar.name,
        "at": at.isoformat(),
        "open": calendar.is_open(at),
        "next_open": (None if calendar.is_open(at) else calendar.next_open(at).isoformat()),
        "square_off": plan.as_dict() if plan else None,
        "costs": compare_models(notional, models),
        "instrument_example": Instrument("EXAMPLE").band(D("1000")),
        "broker": _nse_broker_status(cfg),
    }
    if args.json:
        _print(report)
        return 0
    print(f"venue {report['venue']} at {report['at']}")
    print(f"  open: {report['open']}" + (f" | next open {report['next_open']}" if report["next_open"] else ""))
    if plan:
        print(
            f"  square-off: {plan.reason} | close {plan.session_close.isoformat()} "
            f"| {plan.seconds_left / 60:.0f} min left"
        )
    else:
        print("  square-off: none (continuous market)")
    print(f"  round trip on {notional}:")
    for row in report["costs"]:
        print(f"    {row['model']:8s} total {row['total']:>10s}  ({float(row['rate']) * 100:.3f}% of notional)")
    crypto_rt = round_trip(crypto, entry_notional=notional, exit_notional=notional)
    nse_rt = round_trip(NSECostModel(), entry_notional=notional, exit_notional=notional)
    print(f"  cheapest: {'NSE' if nse_rt.total < crypto_rt.total else 'crypto'} by "
          f"{abs(nse_rt.total - crypto_rt.total):.2f}")
    broker = report["broker"]
    print(f"  NSE broker: {broker['adapter']} - {broker['status']}")
    return 0


def _nse_broker_status(cfg: Config) -> dict[str, Any]:
    """Is there an NSE execution adapter behind the venue seams, and is it configured?

    Saying "Kite Connect adapter implemented, keys missing" is the honest answer; saying
    "NSE ready" while nothing can place an order is not.
    """
    from .execution.adapters.base import ExchangeError
    from .execution.adapters.nse import KiteAdapter

    if not (cfg.secrets.kite_api_key and cfg.secrets.kite_access_token):
        return {
            "adapter": "kite",
            "implemented": True,
            "configured": False,
            "status": "implemented, no keys: set KITE_API_KEY and KITE_ACCESS_TOKEN",
        }
    try:
        adapter = KiteAdapter.from_config(cfg)
    except (ValueError, ExchangeError) as exc:
        return {"adapter": "kite", "implemented": True, "configured": False, "status": str(exc)}

    async def _probe() -> dict[str, Any]:
        try:
            balances = await adapter.get_balances()
            return {"configured": True, "reachable": True, "balances": {k: str(v) for k, v in balances.items()}}
        except ExchangeError as exc:  # a broker that refuses is a fact, not a crash
            return {"configured": True, "reachable": False, "status": f"{type(exc).__name__}: {exc}"}
        finally:
            await adapter.close()

    probe = asyncio.run(_probe())
    probe["adapter"] = "kite"
    probe.setdefault("status", "reachable" if probe.get("reachable") else "unreachable")
    return probe


def cmd_parity(args: argparse.Namespace) -> int:
    from .backtest.parity import format_parity, run_parity

    cfg = _config(args)
    result = asyncio.run(
        run_parity(cfg, start=datetime(2024, 1, 1, tzinfo=UTC), minutes=args.minutes, seed=args.seed)
    )
    if args.json:
        _print(result)
    else:
        print(format_parity(result))
    return 0 if result["passed"] else 1


def _cmd_dashboard_png(args: argparse.Namespace) -> int:
    """Draw the live dashboard to a PNG.

    Useful where a web page cannot be looked at: an embedded preview that will not
    render, a chat that wants an image, or a report for someone without access.
    """
    import json
    import urllib.request

    from .dashboard.render import render_png

    try:
        with urllib.request.urlopen(args.url, timeout=15) as response:
            snapshot = json.load(response)
    except Exception as exc:  # noqa: BLE001 - the message is the point, not the type
        print(f"could not read {args.url}: {exc}", file=sys.stderr)
        print("is the dashboard running? try: python -m tradebot dashboard", file=sys.stderr)
        return 2
    try:
        path = render_png(snapshot, args.out)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote {path} (equity {snapshot.get('equity')}, kill {snapshot.get('kill', {}).get('level')})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "check": cmd_check,
        "backtest": cmd_backtest,
        "run": cmd_run,
        "live-check": cmd_live_check,
        "venue-observe": cmd_venue_observe,
        "pending-fills": cmd_pending_fills,
        "status": cmd_status,
        "metrics": cmd_metrics,
        "report": cmd_report,
        "pause": cmd_control,
        "resume": cmd_control,
        "kill": cmd_control,
        "lockdown": cmd_control,
        "drill": cmd_drill,
        "learn": cmd_learn,
        "dashboard": cmd_dashboard,
        "dashboard-png": _cmd_dashboard_png,
        "walk-forward": cmd_walk_forward,
        "sensitivity": cmd_sensitivity,
        "parity": cmd_parity,
        "venues": cmd_venues,
        "archive": cmd_archive,
        "robustness": cmd_robustness,
        "signals": cmd_signals,
        "slippage": cmd_slippage,
        "meta": cmd_meta,
        "stream": cmd_stream,
        "telegram": cmd_telegram,
        "backfill": cmd_backfill,
        "backfill-history": cmd_backfill_history,
        "health": cmd_health,
        "autonomy": cmd_autonomy,
        "recovery-request": cmd_recovery_request,
        "recovery-apply": cmd_recovery_apply,
        "allocation-request": cmd_allocation_request,
        "allocation-apply": cmd_allocation_apply,
        "strategy-reduction-request": cmd_strategy_reduction_request,
        "strategy-reduction-apply": cmd_strategy_reduction_apply,
        "alerts": cmd_alerts,
        "watchdog": cmd_watchdog,
        "experiments": cmd_experiments,
        "news": cmd_news,
        "research": cmd_research,
    }
    if args.command in ("pause", "resume", "kill", "lockdown"):
        args.action = args.command
    handler = handlers[args.command]
    try:
        return int(handler(args) or 0)
    except (FileNotFoundError, ValueError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
