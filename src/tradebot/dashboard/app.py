"""Local dashboard: FastAPI + one server-rendered page.

Read-only by default. Controls (pause / resume / flatten / kill) need an explicit
confirmation string, exactly like the CLI and Telegram paths, and are disabled
entirely when `controls_enabled` is false.

If `run_bot` is on (the demo default) the app also runs the paper engine in the same
process, on the synthetic feed, so `tradebot dashboard` is a complete working demo.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from ..autonomy import Approvals, AutonomyLedger
from ..config import Config
from ..core.clock import iso
from ..core.money import D, to_str
from ..core.store import Store

TEMPLATE = Path(__file__).parent / "index.html"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class DashboardState:
    """Holds the optional live engine plus a read-only store handle."""

    def __init__(self, config: Config, engine: Any = None) -> None:
        self.config = config
        self.engine = engine
        self.store = engine.store if engine is not None else Store(config.settings.db_path)
        self.password = config.secrets.dashboard_password
        # Opt in explicitly, and never expose unauthenticated mutation endpoints.
        self.controls_enabled = (
            not config.secrets.dashboard_readonly and bool(self.password)
        )
        self._task: asyncio.Task | None = None

    def check_auth(self, request: Request) -> None:
        if not self.password or not secrets.compare_digest(
            request.headers.get("x-dashboard-token", "").encode("utf-8"), self.password.encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="dashboard token required")

    async def stop(self) -> None:
        if self.engine is not None:
            self.engine.running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


def create_app(config: Config, engine: Any = None, *, run_bot: bool = True) -> FastAPI:
    state = DashboardState(config, engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_bot and state.engine is None:
            from ..alerts.notifier import Notifier
            from ..alerts.router import router_from_config
            from ..core.clock import SimClock, timeframe_seconds, utcnow
            from ..data.feed import SyntheticFeed
            from ..engine import Engine

            # The hosted demo must never construct a real venue adapter or silently
            # substitute synthetic data for a caller who requested another feed.
            if config.mode.value != "paper" or config.settings.feed.value != "synthetic":
                raise RuntimeError("hosted demo requires paper mode and the synthetic feed")
            base_seconds = timeframe_seconds(config.settings.base_timeframe)
            # Synthetic ticks are generated in one-minute batches. Applying a 15-second
            # streaming threshold to the previous batch marks healthy symbols stale.
            # This adjustment is restricted to the hosted synthetic paper demo.
            quality = config.settings.data_quality
            quality.stale_symbol_seconds = max(quality.stale_symbol_seconds, 2 * base_seconds)
            quality.stale_all_seconds = max(quality.stale_all_seconds, 3 * base_seconds)

            # A paced simulated clock: the demo advances `synthetic.speed` market
            # seconds per real second, and every timeout, cooldown, staleness window
            # and day boundary is measured on that one timeline.
            speed = max(float(config.settings.synthetic.speed), 1.0)
            # The generator emits a complete bar immediately. Begin with the most
            # recently completed bar so a 1x demo never publishes future timestamps.
            clock = SimClock(utcnow() - timedelta(seconds=base_seconds), pace=speed)
            config.ensure_dirs()
            feed = SyntheticFeed(config, clock)
            notifier = Notifier(config, state.store, router=router_from_config(config, state.store))
            state.engine = await Engine.build(
                config, clock=clock, store=state.store, feed=feed, notifier=notifier
            )
            state.store = state.engine.store
            state._task = asyncio.create_task(state.engine.run(), name="demo-engine")
        yield
        await state.stop()

    app = FastAPI(title="tradebot dashboard", version="0.1.0", lifespan=lifespan)
    app.state.dashboard = state

    # Request accounting: makes "is anything reaching the server?" answerable from the
    # outside when a preview looks blank, instead of guessing from the client side.
    started_at = time.monotonic()
    seen: dict[str, Any] = {
        "total": 0,
        "last_path": "",
        "last_at": None,
        "last_ua": "",
        "last_client": "",
    }

    @app.middleware("http")
    async def _count(request: Request, call_next):
        response = await call_next(request)
        seen["total"] = int(seen["total"]) + 1
        seen["last_path"] = request.url.path
        seen["last_at"] = datetime.now(UTC).isoformat()
        seen["last_ua"] = request.headers.get("user-agent", "")[:80]
        seen["last_client"] = request.headers.get("x-forwarded-for", "") or (
            request.client.host if request.client else ""
        )
        return response

    @app.get("/api/self-test")
    async def self_test() -> JSONResponse:
        """What the server thinks is going on, for diagnosing a blank preview."""
        engine = state.engine
        now = engine.clock.now() if engine is not None else datetime.now(UTC)
        return JSONResponse(
            _json_safe(
                {
                    "ok": True,
                    "uptime_seconds": round(time.monotonic() - started_at, 1),
                    "requests_served": seen["total"],
                    "last_request": {k: seen[k] for k in ("last_path", "last_at", "last_ua", "last_client")},
                    "bot_running": bool(engine is not None and engine.running),
                    "market_clock": iso(now),
                    "engine_stats": engine.stats.to_dict() if engine is not None else {},
                    "kill_level": state.store.kill_level(),
                    "template_bytes": len(TEMPLATE.read_text(encoding="utf-8")),
                }
            ),
            headers={"cache-control": "no-store, max-age=0"},
        )

    # --- page ------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        # no-store: the preview proxies this page, and a cached copy after a restart
        # looks exactly like a broken app. Always hand the browser the live build.
        return HTMLResponse(
            TEMPLATE.read_text(encoding="utf-8"),
            headers={"cache-control": "no-store, max-age=0"},
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok"

    # --- API -------------------------------------------------------------
    @app.get("/api/snapshot")
    async def snapshot() -> JSONResponse:
        engine = state.engine
        store = state.store
        marks: dict[str, Decimal] = {}
        if engine is not None:
            payload = engine.status()
            marks = engine.marks
            positions = payload["open_positions"]
            equity = D(payload["equity"])
        else:
            positions = []
            equity = store.last_equity() or D("0")
            payload = {
                "mode": config.mode.value,
                "kill_level": store.kill_level(),
                "equity": to_str(equity),
                "cash": to_str(equity),
                "realized_pnl": "0",
                "unrealized_pnl": "0",
                "fees": "0",
                "tax_reserve": "0",
                "pending_orders": len(store.open_orders()),
                "stats": {},
                "order_stats": {},
                "ramp": {},
                "now": iso(datetime.now(UTC)),
            }
        curve = store.equity_series(limit=1440)
        start_equity = D(str(store.kv_get("day_start_equity", payload.get("equity", "0"))))
        day_pnl = equity - start_equity
        peak_equity = max(D(str(store.kv_get("peak_equity", str(equity)))), equity)
        drawdown = (equity - peak_equity) / peak_equity if peak_equity > 0 else D("0")
        trades = store.closed_positions(since=datetime.now(UTC) - timedelta(days=30), limit=500)
        by_strategy: dict[str, dict[str, Any]] = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
        for t in trades:
            row = by_strategy[t.get("strategy", "?")]
            pnl = float(D(t.get("pnl") or "0"))
            row["trades"] += 1
            row["pnl"] += pnl
            row["wins"] += int(pnl > 0)
        signals = store.signals(limit=12)
        verdicts = store.verdicts(limit=8)
        events = store.events(limit=40)
        kill_state = store.kill_state()
        health = {
            "disk": _disk_pct(config),
            "symbols": {
                sym: {
                    "price": to_str(engine.market.price(sym)) if engine and engine.market.price(sym) else None,
                    "age": round(engine.market.age_seconds(sym, engine.clock.now()), 1) if engine else None,
                }
                for sym in config.symbol_names()
            },
            "db": store.stats(),
            "components": engine.health.snapshot(now=engine.clock.now()) if engine is not None else {},
        }
        return JSONResponse(
            headers={"cache-control": "no-store, max-age=0"},
            content=_json_safe(
                {
                    "status": payload,
                    "equity": to_str(equity),
                    "day_start_equity": to_str(start_equity),
                    "day_pnl": to_str(day_pnl),
                    "day_pnl_pct": to_str(day_pnl / start_equity if start_equity else D("0")),
                    "peak_equity": to_str(peak_equity),
                    "drawdown_pct": to_str(drawdown),
                    "equity_curve": [
                        {"ts": p["ts"] if isinstance(p["ts"], str) else iso(p["ts"]), "equity": float(p["equity"])} for p in curve
                    ],
                    "positions": positions,
                    "trades": trades[:25],
                    "by_strategy": dict(by_strategy),
                    "signals": [
                        {
                            "strategy": s.get("strategy"),
                            "symbol": s.get("symbol"),
                            "side": s.get("side"),
                            "reason": s.get("reason"),
                            "ts": s.get("ts"),
                            "confidence": s.get("confidence"),
                        }
                        for s in signals
                    ],
                    "verdicts": verdicts[:8],
                    "events": [
                        {"ts": e["ts"], "kind": e["kind"], "level": e["level"], "payload": e["payload"]}
                        for e in events
                    ],
                    "kill": kill_state,
                    "kill_history": store.kill_history(5),
                    "health": health,
                    "autonomy": _autonomy_view(store),
                    "controls_enabled": state.controls_enabled and engine is not None,
                    "limits": {
                        "risk_per_trade": str(config.risk.risk_per_trade),
                        "daily_loss_stop": str(config.risk.daily_loss_stop_new_entries),
                        "daily_loss_flatten": str(config.risk.daily_loss_flatten),
                        "max_drawdown": str(config.risk.max_drawdown_lockdown),
                        "total_exposure": str(config.risk.total_exposure),
                        "max_open_positions": config.risk.max_open_positions,
                    },
                    "marks": {k: to_str(v) for k, v in marks.items()},
                    "slippage": _slippage_view(engine),
                    "veto_breakdown": _veto_view(store),
                    "server_time": iso(datetime.now(UTC)),
                }
            )
        )

    def _autonomy_view(store: Store) -> dict[str, Any]:
        return {
            "execution_recovery": store.execution_recovery_status(),
            "ledger": AutonomyLedger(store=store).summary(),
            "pending_approvals": [request.as_dict() for request in Approvals(store=store).pending()],
            "limitation": "Partial audit coverage; approvals are not authenticated or usable for execution.",
        }

    @app.get("/api/autonomy")
    async def autonomy() -> JSONResponse:
        return JSONResponse(_json_safe(_autonomy_view(state.store)), headers={"cache-control": "no-store"})

    def _slippage_view(engine: Any) -> dict[str, Any]:
        """What execution actually cost, against the price the decision was made at.

        The number that matters is p95, not the mean: a median of zero with a fat tail is
        a strategy that looks fine until it does not.
        """
        if engine is None or not getattr(engine.slippage, "records", None):
            return {"available": False, "note": "no fills with a decision price yet"}
        summary = engine.slippage.summary()
        model = getattr(engine, "slippage_model", None)
        return {
            "available": True,
            "fills": summary["fills"],
            "median_bps": summary["median_bps"],
            "mean_bps": summary["mean_bps"],
            "p95_bps": summary["p95_bps"],
            "worst_bps": summary["worst_bps"],
            "budget_bps": float(config.settings.execution.max_slippage_bps),
            "sizing_model": None
            if model is None
            else {"samples": model.samples, "fallback_bps": round(model.fallback_bps, 2)},
        }

    @app.get("/api/health")
    async def component_health() -> JSONResponse:
        """§20.2: the Health page reads this. A blank payload is a fact, not an error."""
        engine = state.engine
        if engine is None:
            return JSONResponse(
                {"available": False, "note": "no bot attached; start `tradebot run` or the hosted dashboard"},
                headers={"cache-control": "no-store, max-age=0"},
            )
        snapshot = engine.health.snapshot(
            now=engine.clock.now(), restart_budget=getattr(engine, "strategy_faults", None)
        )
        return JSONResponse(
            _json_safe({"available": True, **snapshot}),
            headers={"cache-control": "no-store, max-age=0"},
        )

    @app.get("/api/report/latest", response_class=PlainTextResponse)
    async def latest_report() -> str:
        reports = Path(config.settings.reports_dir)
        if not reports.exists():
            raise HTTPException(404, "no reports yet")
        days = sorted(p for p in reports.iterdir() if p.is_dir())
        if not days:
            raise HTTPException(404, "no reports yet")
        report = days[-1] / "report.md"
        if not report.exists():
            raise HTTPException(404, "report file missing")
        return report.read_text(encoding="utf-8")

    @app.get("/api/reports")
    async def list_reports() -> JSONResponse:
        reports = Path(config.settings.reports_dir)
        out = []
        if reports.exists():
            for day in sorted((p for p in reports.iterdir() if p.is_dir()), reverse=True)[:30]:
                out.append(
                    {
                        "date": day.name,
                        "markdown": (day / "report.md").exists(),
                        "html": (day / "report.html").exists(),
                        "tax_csv": (day / "tax_ledger.csv").exists(),
                    }
                )
        return JSONResponse(out)

    # --- controls ---------------------------------------------------------
    async def _control(action: str, request: Request, reason: str, confirm: str) -> JSONResponse:
        if not state.controls_enabled:
            raise HTTPException(403, "controls disabled; explicit opt-in and password required")
        state.check_auth(request)
        if state.engine is None:
            raise HTTPException(409, "no live engine in this process")
        if action in ("flatten", "kill") and confirm != "CONFIRM":
            raise HTTPException(400, "destructive action requires confirm=CONFIRM")
        if action == "pause":
            await state.engine.pause(reason or "dashboard pause")
        elif action == "resume":
            try:
                await state.engine.resume(reason or "dashboard resume", actor="dashboard", l1_only=True)
            except PermissionError as exc:
                raise HTTPException(409, str(exc)) from exc
        elif action == "flatten":
            # Block new entries before awaiting potentially slow order cancellation/closing.
            await state.engine.pause(reason or "dashboard flatten")
            await state.engine.flatten_all(reason or "dashboard flatten")
        elif action == "kill":
            await state.engine.kill(reason or "dashboard kill")
        else:  # pragma: no cover
            raise HTTPException(400, f"unknown action {action}")
        state.store.log_event("dashboard_control", {"action": action, "reason": reason}, level="WARN", actor="dashboard")
        return JSONResponse({"ok": True, "action": action, "kill_level": state.store.kill_level()})

    @app.post("/api/control/{action}")
    async def control(action: str, request: Request) -> JSONResponse:
        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        return await _control(action, request, str(body.get("reason", "")), str(body.get("confirm", "")))

    return app


def _veto_view(store: Any, *, limit: int = 2000) -> dict[str, Any]:
    """Why entries were refused, by check - the most useful table on the page.

    A bot that trades rarely looks identical to a bot that is broken; the difference
    is here. Counts come from the risk verdict log, which records every rejection with
    the check that failed, so a rising `cost_budget` line means the strategies are
    proposing trades that cannot pay for themselves, not that the venue is down.
    """
    try:
        rows = store.verdicts(limit=limit)
    except Exception:  # noqa: BLE001 - a dashboard must render on a broken DB
        return {"available": False, "note": "risk verdict log unreadable"}
    if not rows:
        return {"available": False, "note": "no risk verdicts logged yet"}
    counts: dict[str, int] = defaultdict(int)
    approved = 0
    for row in rows:
        if row.get("approved"):
            approved += 1
            continue
        check = (row.get("reason") or "unknown").split(":", 1)[0].strip() or "unknown"
        counts[check] += 1
    total = len(rows)
    aggregated: list[dict[str, Any]] = [
        {"check": name, "count": count, "share": round(count / total, 4)}
        for name, count in counts.items()
    ]
    aggregated.sort(key=lambda item: item["count"], reverse=True)
    by_check = aggregated[:10]
    dominant = by_check[0]["check"] if by_check and by_check[0]["share"] >= 0.9 else ""
    note = ""
    if dominant == "kill_switch":
        note = (
            "almost every refusal is the kill switch, not a market condition: entries are "
            "blocked entirely until it is cleared"
        )
    elif dominant:
        note = f"one check accounts for almost all refusals: {dominant}"
    return {
        "available": True,
        "window": total,
        "approved": approved,
        "refused": total - approved,
        "approval_rate": round(approved / total, 4),
        "dominant": dominant,
        "note": note,
        "by_check": by_check,
    }


def _disk_pct(config: Config) -> float:
    import shutil

    try:
        usage = shutil.disk_usage(str(config.settings.data_dir))
        return round(usage.used / usage.total, 3) if usage.total else 0.0
    except OSError:  # pragma: no cover
        return 0.0
