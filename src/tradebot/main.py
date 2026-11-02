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
from typing import Any

from .config import Config, Mode, load_config
from .core.clock import Clock, SimClock, utcnow
from .core.logging import get_logger, setup_logging
from .core.models import KillLevel
from .core.money import D, to_str
from .core.store import Store

log = get_logger("tradebot.main")


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def _config(args: argparse.Namespace) -> Config:
    overrides: dict[str, object] = {}
    risk_overrides: dict[str, object] = {}
    if getattr(args, "mode", None):
        overrides["mode"] = args.mode
    if getattr(args, "feed", None):
        overrides["feed"] = args.feed
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
    cfg = load_config(
        args.config_dir, overrides=overrides or None, risk_overrides=risk_overrides or None
    )
    level = "DEBUG" if getattr(args, "verbose", False) else cfg.settings.logging.level
    setup_logging(level, cfg.settings.logging.json_logs, cfg.settings.logging.file)
    return cfg


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


async def _build_engine(cfg: Config, *, store_path: str | None = None, sim: bool = False, start: datetime | None = None):
    from .data.feed import SyntheticFeed
    from .engine import Engine

    clock = SimClock(start or utcnow()) if sim else Clock()
    store = Store(store_path or cfg.settings.db_path)
    feed = SyntheticFeed(cfg, clock) if cfg.settings.feed.value == "synthetic" else None
    engine = await Engine.build(cfg, clock=clock, store=store, feed=feed)
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


async def _run_backtest(cfg: Config, days: int, seed: int | None = None, capital: Decimal | None = None) -> dict:
    from .engine import Engine

    start = datetime(2024, 1, 1, tzinfo=UTC)
    clock = SimClock(start)
    store = Store(":memory:")
    engine = await Engine.build(cfg, clock=clock, store=store)
    if capital is not None:
        engine.ledger.starting_cash = capital
        engine.adapter.cash = capital  # type: ignore[attr-defined]
        engine.starting_capital = capital
        store.kv_set("day_start_equity", to_str(capital))
        store.kv_set("week_start_equity", to_str(capital))
        store.kv_set("peak_equity", to_str(capital))
    if seed is not None:
        engine.feed.rng.seed(seed)  # type: ignore[attr-defined]
    await engine.run(stop_at=start + timedelta(days=days))
    marks = engine.marks
    result = {
        "mode": "backtest(synthetic)",
        "days": days,
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
    result = asyncio.run(_run_backtest(cfg, args.days, args.seed, D(args.capital) if args.capital else None))
    if args.json:
        _print(result)
    else:
        print(result["report_telegram"])
        print()
        print(f"closed trades: {result['closed_trades']} | win rate {result['win_rate']} | "
              f"fees {result['fees']} | end equity {result['end_equity']}")
        print("reports:", ", ".join(result["report_paths"].values()))
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

    async def _main() -> int:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        listener = _start_command_listener(cfg, engine)

        def _stop(*_: object) -> None:
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # pragma: no cover - windows
                loop.add_signal_handler(sig, _stop)
        run_task = asyncio.create_task(engine.run())
        stop_task = asyncio.create_task(stop.wait())
        await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if not run_task.done():
            engine.running = False
            run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        if listener is not None:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        print(json.dumps(engine.status(), indent=2, default=str))
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
    ks = KillSwitch(store)
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
    parser.add_argument("--feed", choices=["synthetic", "ccxt", "stream"], default=argparse.SUPPRESS)
    parser.add_argument(
        "--speed", type=float, default=argparse.SUPPRESS,
        help="synthetic feed speed (simulated seconds per real second)",
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

    run_parser = sub.add_parser("run", help="start the trading loop (paper by default)")
    run_parser.add_argument("--confirm", default=None, help='live mode: type the capital amount and "LIVE"')

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
    start = datetime(2024, 1, 1, tzinfo=UTC)
    total = args.train_days + args.test_days * max(1, args.windows)
    windows = rolling_windows(
        start,
        start + timedelta(days=total),
        train_days=args.train_days,
        test_days=args.test_days,
    )[: max(1, args.windows)]
    if not windows:
        print("no windows fit in the requested range", file=sys.stderr)
        return 2
    capital = D(args.capital) if args.capital else None
    result = asyncio.run(
        run_walk_forward(cfg, windows, capital=capital, seed=args.seed)
    )
    if args.json:
        _print(
            {
                "windows": result["windows"],
                "summary": result["summary"],
                "metrics": result["metrics_dict"],
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
    start = datetime(2024, 1, 1, tzinfo=UTC)
    report = asyncio.run(
        run_grid(
            cfg,
            strategy=args.strategy,
            axes=axes,
            start=start,
            end=start + timedelta(days=args.days),
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

    async def _session(this_cfg: Config) -> tuple[Any, Any, Any]:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        clock = SimClock(start)
        store = Store(":memory:")
        engine = await Engine.build(this_cfg, clock=clock, store=store)
        if seed is not None:
            engine.feed.rng.seed(seed)  # type: ignore[attr-defined]
        await engine.run(stop_at=start + timedelta(days=days))
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
    report["gates"] = [g.to_dict() for g in base["gates"]]
    report["passed"] = base["passed"]
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
        ladder = run_ladder(frame, horizon=args.horizon, n_folds=args.folds)
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

    start = datetime(2024, 1, 1, tzinfo=UTC)
    clock = SimClock(start)
    store = Store(":memory:")
    engine = await Engine.build(cfg, clock=clock, store=store)
    if seed is not None:
        engine.feed.rng.seed(seed)  # type: ignore[attr-defined]
    await engine.run(stop_at=start + timedelta(days=days))
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
    router = CommandRouter(
        ops=build_ops(engine=engine, store=engine.store, clock=engine.clock),
        whitelist=allowed,
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
    return 0


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
        "status": cmd_status,
        "metrics": cmd_metrics,
        "report": cmd_report,
        "pause": cmd_control,
        "resume": cmd_control,
        "kill": cmd_control,
        "lockdown": cmd_control,
        "drill": cmd_drill,
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
    }
    if args.command in ("pause", "resume", "kill", "lockdown"):
        args.action = args.command
    handler = handlers[args.command]
    try:
        return int(handler(args) or 0)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
