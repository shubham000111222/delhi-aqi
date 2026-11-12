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


def test_dashboard_drawdown_uses_peak_not_day_start(client, monkeypatch):
    store = client.app.state.dashboard.store
    store.kv_set("peak_equity", "1250")
    store.kv_set("day_start_equity", "1000")
    monkeypatch.setattr(store, "last_equity", lambda: D("1000"))
    snapshot = client.get("/api/snapshot").json()
    assert D(snapshot["day_pnl_pct"]) == 0
    assert D(snapshot["drawdown_pct"]) == D("-0.2")


def test_dashboard_health_and_reports_listing(client):
    assert client.get("/healthz").text == "ok"
    health = client.get("/api/health")
    assert health.status_code == 200
    payload = health.json()
    # no engine attached in this process: the endpoint must say so rather than 500
    assert payload["available"] is False
    assert "note" in payload
    assert client.get("/api/reports").status_code == 200
    assert client.get("/api/report/latest").status_code in (200, 404)


def test_dashboard_exposes_health_and_partial_autonomy_audit(client):
    from tradebot.autonomy import AutonomyLedger, AutonomyLevel

    store = client.app.state.dashboard.store
    AutonomyLedger(store=store).record("health", "hold_for_flapping", AutonomyLevel.PROTECT)
    result = client.get("/api/autonomy")
    assert result.status_code == 200
    data = result.json()
    assert data["ledger"]["recent"][-1]["action"] == "hold_for_flapping"
    assert "not authenticated" in data["limitation"]
    snapshot = client.get("/api/snapshot").json()
    assert snapshot["autonomy"]["ledger"]["actions"] == 1
    assert snapshot["controls_enabled"] is False
    page = client.get("/").text
    assert 'id="component-health"' in page
    assert 'id="autonomy"' in page
    assert "right.textContent = String(value)" in page


def test_dashboard_controls_need_confirmation(client):
    assert client.post("/api/control/pause", json={}).status_code == 403
    assert client.post("/api/control/flatten", json={"reason": "test"}).status_code == 403


def test_dashboard_controls_require_opt_in_and_password(fast_cfg):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    engine = SimpleNamespace(store=Store(":memory:"), running=False, pause=AsyncMock())
    fast_cfg = replace(fast_cfg, secrets=replace(fast_cfg.secrets, dashboard_password="test-password"))
    with TestClient(create_app(fast_cfg, engine=engine, run_bot=False)) as c:
        assert c.post("/api/control/pause", headers={"x-dashboard-token": "test-password"}).status_code == 403
    fast_cfg = replace(fast_cfg, secrets=replace(fast_cfg.secrets, dashboard_readonly=False, dashboard_password=""))
    with TestClient(create_app(fast_cfg, engine=engine, run_bot=False)) as c:
        assert c.post("/api/control/pause").status_code == 403
    fast_cfg = replace(fast_cfg, secrets=replace(fast_cfg.secrets, dashboard_password="test-password"))
    with TestClient(create_app(fast_cfg, engine=engine, run_bot=False)) as c:
        assert c.post("/api/control/pause").status_code == 401
        assert c.post("/api/control/pause", headers={"x-dashboard-token": "wrong"}).status_code == 401
        assert c.post("/api/control/pause", headers={"x-dashboard-token": "test-password"}).status_code == 200
    engine.pause.assert_awaited_once()


def test_dashboard_readonly_dotenv_and_environment_precedence(fast_cfg):
    from tradebot.config import load_config

    config_dir = fast_cfg.settings.config_dir
    (config_dir.parent / ".env").write_text("DASHBOARD_READONLY=false\nDASHBOARD_PASSWORD=test-only\n")
    enabled = load_config(config_dir, environ={})
    assert enabled.secrets.dashboard_readonly is False
    assert enabled.secrets.dashboard_password == "test-only"
    disabled = load_config(config_dir, environ={"DASHBOARD_READONLY": "true"})
    assert disabled.secrets.dashboard_readonly is True


def test_dashboard_flatten_pauses_before_close(fast_cfg):
    from dataclasses import replace
    from types import SimpleNamespace

    operations = []

    async def pause(reason):
        operations.append("pause")

    async def flatten(reason):
        operations.append("flatten")

    engine = SimpleNamespace(store=Store(":memory:"), running=False, pause=pause, flatten_all=flatten)
    fast_cfg = replace(fast_cfg, secrets=replace(fast_cfg.secrets, dashboard_password="test-password"))
    fast_cfg = replace(fast_cfg, secrets=replace(fast_cfg.secrets, dashboard_readonly=False))
    with TestClient(create_app(fast_cfg, engine=engine, run_bot=False)) as c:
        headers = {"x-dashboard-token": "test-password"}
        assert c.post("/api/control/flatten", headers=headers, json={}).status_code == 400
        assert operations == []
        assert c.post("/api/control/flatten", headers=headers, json={"confirm": "CONFIRM"}).status_code == 200
    assert operations == ["pause", "flatten"]


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


def test_hosted_paper_demo_has_alert_floor_and_no_future_market_clock(fast_cfg):
    fast_cfg.settings.synthetic.speed = 1
    app = create_app(fast_cfg, run_bot=True)
    with TestClient(app) as c:
        engine = app.state.dashboard.engine
        assert engine is not None
        assert engine.notifier is not None
        assert engine.notifier.router is not None
        import asyncio

        asyncio.run(engine.notifier.warn("dashboard alert floor check", key="dashboard-floor"))
        alert_log = engine.cfg.settings.state_dir / engine.cfg.settings.watchdog.alert_log
        assert "dashboard alert floor check" in alert_log.read_text()
        assert engine.cfg.settings.data_quality.stale_symbol_seconds >= 120
        assert engine.clock.now() <= datetime.now(UTC)
        assert c.get("/api/self-test").json()["bot_running"]


def test_hosted_demo_refuses_non_paper_mode(fast_cfg):
    from tradebot.config import Mode

    fast_cfg.settings.mode = Mode.LIVE
    with pytest.raises(RuntimeError, match="requires paper"), TestClient(create_app(fast_cfg, run_bot=True)):
        pass


def test_dashboard_reports_refused_resume_as_conflict(fast_cfg):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    engine = SimpleNamespace(
        store=Store(":memory:"), running=False,
        resume=AsyncMock(side_effect=PermissionError("cannot reset while KILL file is present")),
    )
    fast_cfg = replace(fast_cfg, secrets=replace(fast_cfg.secrets, dashboard_password="test-password"))
    with TestClient(create_app(fast_cfg, engine=engine, run_bot=False)) as c:
        c.app.state.dashboard.controls_enabled = True
        response = c.post("/api/control/resume", headers={"x-dashboard-token": "test-password"})
        assert response.status_code == 409
        assert "KILL file" in response.json()["detail"]
    engine.store.close()


def test_dashboard_resume_requests_l1_only(fast_cfg):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    engine = SimpleNamespace(store=Store(":memory:"), running=False, resume=AsyncMock())
    fast_cfg = replace(fast_cfg, secrets=replace(
        fast_cfg.secrets, dashboard_password="test-password", dashboard_readonly=False,
    ))
    with TestClient(create_app(fast_cfg, engine=engine, run_bot=False)) as c:
        response = c.post("/api/control/resume", headers={"x-dashboard-token": "test-password"})
        assert response.status_code == 200
    engine.resume.assert_awaited_once_with("dashboard resume", actor="dashboard", l1_only=True)
    engine.store.close()


def test_dashboard_recovery_status_does_not_treat_l0_as_entry_permission(client):
    store = client.app.state.dashboard.store
    store.kv_set('fill_pending:one', {'corrupt': True})
    store.kv_set('fill_pending:two', 'unreadable')
    store.kv_set('kill_incident_pending:one', 'bad')
    store.kv_set('execution_observation_fault', {'order_id': '<script>private-order</script>'})
    data = client.get('/api/autonomy').json()['execution_recovery']
    assert data['recovery_required'] is True
    assert data['pending_fill_callbacks'] == 2
    assert data['pending_kill_incidents'] == 1
    assert data['invalid_execution_observation'] is True
    assert data['usable_for_reset'] is False
    assert data['reconciliation_verified'] is False
    assert 'private-order' not in str(data)
    assert client.get('/api/snapshot').json()['autonomy']['execution_recovery'] == data
    assert store.has_pending_fill(), 'read endpoints must not release a hold'


def test_empty_recovery_status_is_not_a_reconciliation_certificate(client):
    data = client.get('/api/autonomy').json()['execution_recovery']
    assert data['recovery_required'] is False
    assert data['usable_for_reset'] is False
    assert data['reconciliation_verified'] is False
    assert 'not entry permission' in data['limitation']
    assert 'No listed holds — not entry permission' in client.get('/').text


def test_dashboard_exposes_persisted_timemachine_ticket_not_sample_odds(client):
    store = client.app.state.dashboard.store
    store.log_event('timemachine_check', {'trade_id': '<candidate>', 'verdict': 'SKIP',
                    'multiplier': 0, 'research_only': True, 'reason': 'short history'})
    result = client.get('/api/snapshot').json()['timemachine']
    assert result['checks'][0]['check']['trade_id'] == '<candidate>'
    assert 'unavailable' in result['checks'][0]['ticket']
    assert result['filter_enabled'] is False
    page = client.get('/').text
    assert 'id="timemachine"' in page
    assert 'esc(row.ticket)' in page


def test_daily_report_includes_only_day_checks_and_computed_tickets(fast_cfg):
    from datetime import timedelta

    store = Store(':memory:')
    start = datetime(2024, 1, 1, tzinfo=UTC)
    store.log_event('timemachine_check', {'trade_id': 'today', 'verdict': 'DOWN', 'multiplier': .5,
                    'simulation': {'expected_net_r': .08}}, ts=start)
    store.log_event('timemachine_check', {'trade_id': 'tomorrow', 'multiplier': 0},
                    ts=start + timedelta(days=1))
    try:
        report = build_daily_report(store, fast_cfg, day_start=start, day_end=start + timedelta(days=1),
                                    start_equity=D(1000), end_equity=D(1000), ledger_cash=D(1000),
                                    realized=D(0), unrealized=D(0), fees=D(0), tax_reserve=D(0), tds=D(0),
                                    peak_equity=D(1000), marks={}, open_positions=[])
        assert len(report.to_dict()['timemachine']['checks']) == 1
        assert 'today' in report.markdown() and 'tomorrow' not in report.markdown()
        assert '0.080 R' in report.markdown() and 'today' in report.telegram()
        assert 'Time Machine' in report.html()
    finally:
        store.close()
