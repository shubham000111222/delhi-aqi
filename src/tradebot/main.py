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
    parser.add_argument("--feed", choices=["synthetic", "ccxt"], default=argparse.SUPPRESS)
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
    if args.json:
        _print(report)
    else:
        print(format_sensitivity(report))
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
