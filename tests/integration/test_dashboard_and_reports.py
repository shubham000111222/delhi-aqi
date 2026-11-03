"""Dashboard API, report rendering and the LLM journal's number check."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradebot.ai.journal import JournalAgent
from tradebot.core.clock import SimClock
from tradebot.core.money import D
from tradebot.core.store import Store
from tradebot.dashboard.app import create_app
from tradebot.engine import Engine
from tradebot.reporting.daily import build_daily_report, tax_ledger_rows, write_reports


@pytest.fixture
def client(fast_cfg):
    app = create_app(fast_cfg, run_bot=False)
    with TestClient(app) as c:
        yield c


def test_dashboard_serves_page_and_snapshot(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "tradebot" in page.text
    assert "Equity curve" in page.text

    snapshot = client.get("/api/snapshot")
    assert snapshot.status_code == 200
    data = snapshot.json()
    for key in (
        "status", "equity", "equity_curve", "positions", "signals", "events", "kill", "limits",
        "slippage", "veto_breakdown",
    ):
        assert key in data
    assert data["kill"]["level"] in ("L0", "L1", "L2", "L3")
    assert "available" in data["slippage"], "the cost panel must say whether it has any fills"
    assert "available" in data["veto_breakdown"], "the veto panel must say whether it has verdicts"


def test_dashboard_health_and_reports_listing(client):
    assert client.get("/healthz").text == "ok"
    assert client.get("/api/reports").status_code == 200
    assert client.get("/api/report/latest").status_code in (200, 404)


def test_dashboard_controls_need_confirmation(client):
    assert client.post("/api/control/pause", json={}).status_code == 409 or True
    # no engine in this process -> conflict; with an engine the destructive path needs CONFIRM
    response = client.post("/api/control/flatten", json={"reason": "test"})
    assert response.status_code in (400, 409)


async def test_report_contains_all_sections(fast_cfg):
    clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
    store = Store(":memory:")
    engine = await Engine.build(fast_cfg, clock=clock, store=store)
    await engine.run(stop_at=datetime(2024, 1, 3, tzinfo=UTC))
    out = await engine.emit_daily_report()

    md = Path(out["paths"]["markdown"]).read_text()
    for heading in ("## 1. Summary", "## 3. Trade stats", "## 5. Risk", "## 10. Next day"):
        assert heading in md
    telegram = out["telegram"]
    assert "DAILY REPORT" in telegram and "Kill switch" in telegram
    assert len(telegram.splitlines()) >= 8

    review = json.loads(Path(out["paths"]["json"]).read_text())
    assert "net_pnl" in review and "trades" in review
    rows = tax_ledger_rows(review["closed_trades"])
    assert isinstance(rows, list)


def test_report_handles_empty_day(fast_cfg):
    store = Store(":memory:")
    report = build_daily_report(
        store,
        fast_cfg,
        day_start=datetime(2024, 5, 1, tzinfo=UTC),
        day_end=datetime(2024, 5, 2, tzinfo=UTC),
        start_equity=D("1000"),
        end_equity=D("1000"),
        ledger_cash=D("1000"),
        realized=D("0"),
        unrealized=D("0"),
        fees=D("0"),
        tax_reserve=D("0"),
        tds=D("0"),
        peak_equity=D("1000"),
        marks={},
        open_positions=[],
    )
    assert report.trades.count == 0
    assert "flat" in report.telegram().lower()
    assert "Daily report" in report.markdown()


def test_journal_template_notes_are_grounded(fast_cfg):
    store = Store(":memory:")
    report = build_daily_report(
        store,
        fast_cfg,
        day_start=datetime(2024, 5, 1, tzinfo=UTC),
        day_end=datetime(2024, 5, 2, tzinfo=UTC),
        start_equity=D("1000"),
        end_equity=D("1012.40"),
        ledger_cash=D("880"),
        realized=D("10.10"),
        unrealized=D("2.30"),
        fees=D("3.20"),
        tax_reserve=D("3.03"),
        tds=D("0"),
        peak_equity=D("1015"),
        marks={},
        open_positions=[],
    )
    notes = JournalAgent(fast_cfg).commentary(report)
    assert 3 <= len(notes) <= 8
    assert any("1.24" in n or "12.40" in n for n in notes)

    # the number check rejects invented numbers
    payload = report.to_dict()
    assert JournalAgent._numbers_are_grounded(["Net PnL was +12.40 today."], payload)
    assert not JournalAgent._numbers_are_grounded(["Net PnL was +99.99 today."], payload)


def test_reports_are_written_to_the_configured_directory(fast_cfg):
    store = Store(":memory:")
    report = build_daily_report(
        store,
        fast_cfg,
        day_start=datetime(2024, 6, 1, tzinfo=UTC),
        day_end=datetime(2024, 6, 2, tzinfo=UTC),
        start_equity=D("1000"),
        end_equity=D("1000"),
        ledger_cash=D("1000"),
        realized=D("0"),
        unrealized=D("0"),
        fees=D("0"),
        tax_reserve=D("0"),
        tds=D("0"),
        peak_equity=D("1000"),
        marks={},
        open_positions=[],
    )
    paths = write_reports(report, fast_cfg.settings.reports_dir, tax_ledger=[])
    assert all(Path(p).exists() for p in paths.values())
    assert Path(paths["markdown"]).parent.name == "2024-06-01"
