"""Local dashboard: FastAPI + one server-rendered page.

Read-only by default. Controls (pause / resume / flatten / kill) need an explicit
confirmation string, exactly like the CLI and Telegram paths, and are disabled
entirely when `controls_enabled` is false.

If `run_bot` is on (the demo default) the app also runs the paper engine in the same
process, on the synthetic feed, so `tradebot dashboard` is a complete working demo.
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

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
        self.controls_enabled = os.environ.get("DASHBOARD_READONLY", "").lower() not in ("1", "true")
        self.password = config.secrets.dashboard_password
        self._task: asyncio.Task | None = None

    def check_auth(self, request: Request) -> None:
        if self.password and request.headers.get("x-dashboard-token") != self.password:
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
            from ..core.clock import SimClock, utcnow
            from ..data.feed import SyntheticFeed
            from ..engine import Engine

            # A paced simulated clock: the demo advances `synthetic.speed` market
            # seconds per real second, and every timeout, cooldown, staleness window
            # and day boundary is measured on that one timeline.
            speed = max(float(config.settings.synthetic.speed), 1.0)
            clock = SimClock(utcnow(), pace=speed)
            config.ensure_dirs()
            feed = SyntheticFeed(config, clock)
            state.engine = await Engine.build(config, clock=clock, feed=feed)
            state.store = state.engine.store
            state._task = asyncio.create_task(state.engine.run(), name="demo-engine")
        yield
        await state.stop()

    app = FastAPI(title="tradebot dashboard", version="0.1.0", lifespan=lifespan)
    app.state.dashboard = state

    # --- page ------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return TEMPLATE.read_text(encoding="utf-8")

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
        }
        return JSONResponse(
            _json_safe(
                {
                    "status": payload,
                    "equity": to_str(equity),
                    "day_start_equity": to_str(start_equity),
                    "day_pnl": to_str(day_pnl),
                    "day_pnl_pct": to_str(day_pnl / start_equity if start_equity else D("0")),
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
                    "controls_enabled": state.controls_enabled,
                    "limits": {
                        "risk_per_trade": str(config.risk.risk_per_trade),
                        "daily_loss_stop": str(config.risk.daily_loss_stop_new_entries),
                        "daily_loss_flatten": str(config.risk.daily_loss_flatten),
                        "max_drawdown": str(config.risk.max_drawdown_lockdown),
                        "total_exposure": str(config.risk.total_exposure),
                        "max_open_positions": config.risk.max_open_positions,
                    },
                    "marks": {k: to_str(v) for k, v in marks.items()},
                    "server_time": iso(datetime.now(UTC)),
                }
            )
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
            raise HTTPException(403, "controls disabled (DASHBOARD_READONLY)")
        state.check_auth(request)
        if state.engine is None:
            raise HTTPException(409, "no live engine in this process")
        if action in ("flatten", "kill") and confirm != "CONFIRM":
            raise HTTPException(400, "destructive action requires confirm=CONFIRM")
        if action == "pause":
            await state.engine.pause(reason or "dashboard pause")
        elif action == "resume":
            await state.engine.resume(reason or "dashboard resume")
        elif action == "flatten":
            await state.engine.flatten_all(reason or "dashboard flatten")
            await state.engine.pause(reason or "dashboard flatten")
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


def _disk_pct(config: Config) -> float:
    import shutil

    try:
        usage = shutil.disk_usage(str(config.settings.data_dir))
        return round(usage.used / usage.total, 3) if usage.total else 0.0
    except OSError:  # pragma: no cover
        return 0.0
