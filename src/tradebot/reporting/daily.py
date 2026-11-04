"""Daily summary report: the 11 sections from section 10, plus the tax ledger CSV.

Two renderers share one data structure:
  * a template report (markdown/html/CSV) that always works and never invents a number;
  * a Telegram-sized summary (~20 lines).

The optional LLM journal (ai/journal.py) may add commentary on top, but if it is
unavailable or produces a number that does not match the data, the template output
is what gets sent.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..config import Config
from ..core.clock import iso, utc
from ..core.models import Position
from ..core.money import ZERO, D, to_str
from ..core.store import Store


@dataclass
class TradeStats:
    count: int = 0
    wins: int = 0
    losses: int = 0
    gross_profit: Decimal = ZERO
    gross_loss: Decimal = ZERO
    avg_r: Decimal = ZERO
    best: dict[str, Any] | None = None
    worst: dict[str, Any] | None = None
    fees: Decimal = ZERO
    avg_hold_minutes: float = 0.0

    @property
    def win_rate(self) -> Decimal:
        return D(self.wins) / D(self.count) if self.count else ZERO

    @property
    def profit_factor(self) -> Decimal:
        if self.gross_loss == 0:
            return D("99") if self.gross_profit > 0 else ZERO
        return self.gross_profit / abs(self.gross_loss)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": to_str(self.win_rate, 4),
            "profit_factor": to_str(self.profit_factor, 3),
            "avg_r": to_str(self.avg_r, 3),
            "gross_profit": to_str(self.gross_profit, 6),
            "gross_loss": to_str(self.gross_loss, 6),
            "fees": to_str(self.fees, 6),
            "avg_hold_minutes": round(self.avg_hold_minutes, 1),
            "best": self.best,
            "worst": self.worst,
        }


def _signed(value: Any, places: int = 2) -> str:
    """Signed money for a table cell, from a Decimal, a string (as stored) or a float.

    Report tables are built from dictionaries that mix live values and rows read back
    out of SQLite, where money arrives as text. ``f"{value:+.2f}"`` raises
    ``Sign not allowed in string format specifier`` on a string, which used to take
    the whole engine down at the first midnight.
    """
    try:
        return f"{D(value):+,.{places}f}"
    except (TypeError, ValueError, ArithmeticError):  # pragma: no cover - defensive
        return str(value)


@dataclass
class DailyReport:
    date: str
    mode: str
    kill_level: str
    start_equity: Decimal
    end_equity: Decimal
    peak_equity: Decimal
    realized: Decimal
    unrealized: Decimal
    fees: Decimal
    tax_reserve: Decimal
    tds: Decimal
    cash: Decimal
    trades: TradeStats = field(default_factory=TradeStats)
    by_strategy: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_symbol: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_intraday_drawdown: Decimal = ZERO
    peak_exposure_pct: Decimal = ZERO
    limits_hit: list[str] = field(default_factory=list)
    cooldowns: list[str] = field(default_factory=list)
    size_reductions: list[str] = field(default_factory=list)
    events: dict[str, Any] = field(default_factory=dict)
    kill_events: list[dict[str, Any]] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    next_day: list[str] = field(default_factory=list)
    system_health: dict[str, Any] = field(default_factory=dict)
    ai_notes: list[str] = field(default_factory=list)
    ai_source: str = "template"

    def _health_line(self) -> str:
        """§20.2: the report says which component is unhealthy, not just that it is.

        An overall verdict with no component detail is the least useful line a health
        system can produce: it tells the reader something is wrong and nothing else.
        """
        health = self.system_health or {}
        if not health:
            return "not sampled (no health check has run in this session)"
        overall = str(health.get("overall", "UNKNOWN"))
        components = health.get("components") or {}
        bad = [
            f"{name} {info.get('state')}"
            for name, info in sorted(components.items())
            if info.get("state") != "HEALTHY"
        ]
        line = f"{overall} ({health.get('score', '?')})"
        if bad:
            line += " - " + ", ".join(bad)
        flapping = health.get("flapping") or []
        if flapping:
            line += f" | flapping: {', '.join(flapping)}"
        return line

    @property
    def net_pnl(self) -> Decimal:
        return self.end_equity - self.start_equity

    @property
    def net_pnl_pct(self) -> Decimal:
        return self.net_pnl / self.start_equity if self.start_equity else ZERO

    @property
    def post_tax_pnl(self) -> Decimal:
        return self.net_pnl - self.tax_reserve

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in list(data.items()):
            if isinstance(value, Decimal):
                data[key] = to_str(value, 8)
        data["net_pnl"] = to_str(self.net_pnl, 8)
        data["net_pnl_pct"] = to_str(self.net_pnl_pct, 6)
        data["post_tax_pnl"] = to_str(self.post_tax_pnl, 8)
        return data

    # --- renderers --------------------------------------------------------
    def telegram(self) -> str:
        sign = "+" if self.net_pnl >= 0 else ""
        lines = [
            f"DAILY REPORT | {self.date} UTC | {self.mode.upper()} | Kill switch {self.kill_level}",
            f"Equity {self.end_equity:,.2f} (start {self.start_equity:,.2f}) | "
            f"Net {sign}{self.net_pnl:,.2f} ({sign}{self.net_pnl_pct * 100:,.2f}%)",
            f"Realized {self.realized:+,.2f} | Unrealized {self.unrealized:+,.2f} | "
            f"Fees {-self.fees:,.2f} | Tax reserve {self.tax_reserve:,.2f}",
            f"Trades {self.trades.count} (W{self.trades.wins} / L{self.trades.losses}) | "
            f"Win {self.trades.win_rate * 100:.0f}% | PF {self.trades.profit_factor:.2f} | "
            f"Avg R {self.trades.avg_r:+.2f}",
        ]
        if self.trades.best:
            lines.append(
                f"Best: {self.trades.best['symbol']} +{self.trades.best['pnl']} "
                f"({self.trades.best['strategy']}) | Worst: "
                f"{self.trades.worst['symbol']} {self.trades.worst['pnl']}"
                if self.trades.worst
                else f"Best: {self.trades.best['symbol']} +{self.trades.best['pnl']}"
            )
        if self.by_strategy:
            lines.append(
                "By strategy: "
                + " | ".join(f"{k.split('_')[0]} {_signed(v['pnl'])}" for k, v in self.by_strategy.items())
            )
        lines.append(
            f"Risk: max intraday DD {self.max_intraday_drawdown * 100:+.2f}% | "
            f"peak exposure {self.peak_exposure_pct * 100:.0f}% | "
            f"limits hit: {', '.join(self.limits_hit) if self.limits_hit else 'none'}"
        )
        lines.append(
            f"Events: {self.events.get('reconnects', 0)} reconnects | {self.events.get('gaps', 0)} gaps | "
            f"{self.events.get('rejects', 0)} rejects | recon {self.events.get('reconciliation', 'ok')}"
        )
        if self.open_positions:
            lines.append(
                "Open: "
                + " | ".join(
                    f"{p['symbol']} {p['strategy']} (stop {p['stop']})" for p in self.open_positions[:5]
                )
            )
        else:
            lines.append("Open: flat")
        if self.kill_events:
            lines.append(f"Kill switch: {self.kill_events[0]['level']} - {self.kill_events[0].get('trigger')}")
        lines.append(f"AI notes: {len(self.ai_notes)} bullets")
        lines.append("Tomorrow: " + ("; ".join(self.next_day) if self.next_day else "all strategies active"))
        return "\n".join(lines)

    def markdown(self) -> str:
        t = self.trades
        md = [
            f"# Daily report - {self.date} (UTC)",
            "",
            f"**Mode:** {self.mode} &nbsp;&nbsp; **Kill switch:** {self.kill_level}",
            "",
            "## 1. Summary",
            "",
            "| Item | Value |",
            "| --- | --- |",
            f"| Start equity | {self.start_equity:,.2f} |",
            f"| End equity | {self.end_equity:,.2f} |",
            f"| Net PnL | {self.net_pnl:+,.2f} ({self.net_pnl_pct * 100:+.2f}%) |",
            f"| Post-tax PnL (30% reserve) | {self.post_tax_pnl:+,.2f} |",
            "",
            "## 2. PnL split",
            "",
            "| Realized | Unrealized | Fees | Tax reserve | TDS (1% on sales) | Cash |",
            "| --- | --- | --- | --- | --- | --- |",
            f"| {self.realized:+,.2f} | {self.unrealized:+,.2f} | {-self.fees:,.2f} | "
            f"{self.tax_reserve:,.2f} | {self.tds:,.2f} | {self.cash:,.2f} |",
            "",
            "## 3. Trade stats",
            "",
            f"- Trades: **{t.count}** (wins {t.wins} / losses {t.losses})",
            f"- Win rate: **{t.win_rate * 100:.1f}%**, profit factor **{t.profit_factor:.2f}**, "
            f"avg R **{t.avg_r:+.2f}**",
            f"- Best: {t.best if t.best else 'n/a'}",
            f"- Worst: {t.worst if t.worst else 'n/a'}",
            f"- Fees paid: {t.fees:,.2f}, average hold {t.avg_hold_minutes:.0f} min",
            "",
            "## 4. Breakdown",
            "",
            "| Strategy | Trades | PnL | Wins | Win rate |",
            "| --- | --- | --- | --- | --- |",
        ]
        for name, row in self.by_strategy.items():
            md.append(
                f"| {name} | {row['trades']} | {_signed(row['pnl'])} | {row['wins']} | {row['win_rate']} |"
            )
        md += ["", "| Symbol | Trades | PnL |", "| --- | --- | --- |"]
        for name, row in self.by_symbol.items():
            md.append(f"| {name} | {row['trades']} | {_signed(row['pnl'])} |")

        md += [
            "",
            "## 5. Risk",
            "",
            f"- Max intraday drawdown: {self.max_intraday_drawdown * 100:+.2f}%",
            f"- Peak exposure: {self.peak_exposure_pct * 100:.1f}% of equity",
            f"- Limits hit: {', '.join(self.limits_hit) if self.limits_hit else 'none'}",
            f"- Cooldowns: {', '.join(self.cooldowns) if self.cooldowns else 'none'}",
            f"- Size reductions: {', '.join(self.size_reductions) if self.size_reductions else 'none'}",
            "",
            "## 6. System health",
            "",
            f"- Uptime bars processed: {self.events.get('bars', 0)}",
            f"- Feed gaps: {self.events.get('gaps', 0)}, reconnects: {self.events.get('reconnects', 0)}",
            f"- API errors: {self.events.get('api_errors', 0)}, rejects: {self.events.get('rejects', 0)}",
            f"- Reconciliation: {self.events.get('reconciliation', 'ok')}",
            f"- Component health: {self._health_line()}",
            "",
            "## 7. Kill switch and incidents",
            "",
        ]
        if self.kill_events:
            for ev in self.kill_events:
                md.append(f"- {ev['ts']} **{ev['level']}** ({ev.get('previous')}) - {ev.get('trigger')}: {ev.get('reason')}")
        else:
            md.append("- No kill switch events.")
        for inc in self.incidents:
            md.append(f"- Incident {inc['ts']} [{inc['level']}] {inc['summary']}")

        md += ["", "## 8. Open positions", ""]
        if self.open_positions:
            md += ["| Symbol | Strategy | Qty | Entry | Stop | Target | Unrealized |", "| --- | --- | --- | --- | --- | --- | --- |"]
            for p in self.open_positions:
                md.append(
                    f"| {p['symbol']} | {p['strategy']} | {p['qty']} | {p['avg_entry']} | {p['stop']} | "
                    f"{p['target']} | {p.get('unrealized', 'n/a')} |"
                )
        else:
            md.append("- Flat.")

        md += ["", "## 9. AI commentary", ""]
        md += [f"- {note}" for note in self.ai_notes] if self.ai_notes else ["- (template only)"]
        md += ["", "## 10. Next day", ""]
        md += [f"- {n}" for n in self.next_day] if self.next_day else ["- All strategies active."]
        md += ["", f"_Generated {iso(utc(datetime.now()))} by tradebot ({self.ai_source} commentary)._"]
        return "\n".join(md)

    def html(self) -> str:
        md = self.markdown()
        rows = "\n".join(
            f"<tr><td>{p['symbol']}</td><td>{p['strategy']}</td><td>{p['qty']}</td>"
            f"<td>{p['avg_entry']}</td><td>{p['stop']}</td><td>{p['target']}</td></tr>"
            for p in self.open_positions
        )
        body = md.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br/>")
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<title>tradebot daily report {self.date}</title>
<style>
 body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 32px; color: #101828; }}
 h1,h2 {{ font-weight: 600; }} table {{ border-collapse: collapse; margin: 12px 0; }}
 td,th {{ border: 1px solid #e4e7ec; padding: 6px 10px; font-size: 13px; }}
 th {{ background: #f9fafb; text-align: left; }}
 .badge {{ display:inline-block; padding:2px 8px; border-radius:10px; background:#ecfdf3; color:#067647; font-size:12px; }}
 pre {{ white-space: pre-wrap; font-size: 13px; background:#f9fafb; padding:16px; border-radius:8px; }}
</style></head>
<body>
<h1>Daily report - {self.date} (UTC) <span class="badge">{self.mode}</span></h1>
<p>Kill switch: <strong>{self.kill_level}</strong> &nbsp; Equity: <strong>{self.end_equity:,.2f}</strong>
 ({self.net_pnl:+,.2f} / {self.net_pnl_pct * 100:+.2f}%)</p>
<pre>{body}</pre>
<h2>Open positions</h2>
<table><tr><th>Symbol</th><th>Strategy</th><th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th></tr>
{rows or '<tr><td colspan="6">flat</td></tr>'}</table>
</body></html>"""


def build_daily_report(
    store: Store,
    config: Config,
    *,
    day_start: datetime,
    day_end: datetime,
    start_equity: Decimal,
    end_equity: Decimal,
    ledger_cash: Decimal,
    realized: Decimal,
    unrealized: Decimal,
    fees: Decimal,
    tax_reserve: Decimal,
    tds: Decimal,
    peak_equity: Decimal,
    marks: dict[str, Decimal],
    open_positions: list[Position],
    reconciliation: str = "ok",
    feed_gaps: int = 0,
    reconnects: int = 0,
    system_health: dict[str, Any] | None = None,
) -> DailyReport:
    closed = store.closed_positions(since=day_start, limit=5000)
    report = DailyReport(
        date=utc(day_start).date().isoformat(),
        mode=config.mode.value,
        kill_level=store.kill_level(),
        start_equity=start_equity,
        end_equity=end_equity,
        peak_equity=peak_equity,
        realized=realized,
        unrealized=unrealized,
        fees=fees,
        tax_reserve=tax_reserve,
        tds=tds,
        cash=ledger_cash,
    )

    stats = TradeStats()
    by_strategy: dict[str, dict[str, Any]] = {}
    by_symbol: dict[str, dict[str, Any]] = {}
    holds: list[float] = []
    for trade in closed:
        pnl = D(trade.get("pnl") or "0")
        stats.count += 1
        if pnl > 0:
            stats.wins += 1
            stats.gross_profit += pnl
        elif pnl < 0:
            stats.losses += 1
            stats.gross_loss += pnl
        stats.fees += D(trade.get("fees") or "0")
        r = D(trade.get("r_multiple") or "0")
        stats.avg_r += r
        item = {
            "symbol": trade.get("symbol"),
            "strategy": trade.get("strategy"),
            "pnl": to_str(pnl, 2),
            "r": to_str(r, 2),
            "opened_at": trade.get("opened_at"),
            "closed_at": trade.get("closed_at"),
            "reason": trade.get("exit_reason"),
        }
        if stats.best is None or pnl > D(stats.best["pnl"]):
            stats.best = item
        if stats.worst is None or pnl < D(stats.worst["pnl"]):
            stats.worst = item
        for key, bucket, field_name in (
            (trade.get("strategy", "unknown"), by_strategy, "strategy"),
            (trade.get("symbol", "unknown"), by_symbol, "symbol"),
        ):
            row = bucket.setdefault(key, {"trades": 0, "wins": 0, "pnl": ZERO, "fees": ZERO})
            row["trades"] += 1
            row["wins"] += int(pnl > 0)
            row["pnl"] += pnl
            row["fees"] += D(trade.get("fees") or "0")
            del field_name
        try:
            opened = datetime.fromisoformat(str(trade.get("opened_at")).replace("Z", "+00:00"))
            closed_at = datetime.fromisoformat(str(trade.get("closed_at")).replace("Z", "+00:00"))
            holds.append((closed_at - opened).total_seconds() / 60.0)
        except (TypeError, ValueError):
            pass
    if stats.count:
        stats.avg_r = stats.avg_r / stats.count
        stats.avg_hold_minutes = sum(holds) / len(holds) if holds else 0.0
    report.trades = stats
    report.closed_trades = closed
    report.by_strategy = {k: _fmt_row(v) for k, v in by_strategy.items()}
    report.by_symbol = {k: _fmt_row(v) for k, v in by_symbol.items()}

    # equity curve: intraday drawdown and peak exposure
    curve = store.equity_series(since=day_start, limit=10000)
    peak = start_equity
    max_dd = ZERO
    peak_exposure = ZERO
    for point in curve:
        eq = point["equity"]
        peak = max(peak, eq)
        if peak > 0:
            max_dd = min(max_dd, (eq - peak) / peak)
        if eq > 0:
            peak_exposure = max(peak_exposure, point["positions_value"] / eq)
    report.max_intraday_drawdown = max_dd
    report.peak_exposure_pct = peak_exposure

    # events and health
    events = store.events(limit=2000, since=day_start)
    counts: dict[str, int] = {}
    for ev in events:
        counts[ev["kind"]] = counts.get(ev["kind"], 0) + 1
    report.events = {
        "bars": counts.get("bar_closed", 0),
        "gaps": feed_gaps or counts.get("data_gap", 0),
        "reconnects": reconnects or counts.get("feed_reconnect", 0),
        "api_errors": counts.get("order_error", 0),
        "rejects": counts.get("order_rejected", 0),
        "reconciliation": reconciliation,
        "orders": counts.get("order_submitted", 0),
        "signals": counts.get("signal", 0),
        "vetoes": counts.get("risk_veto", 0),
        "kill_switch": counts.get("kill_switch", 0),
    }
    report.kill_events = [
        e for e in store.kill_history(50)
        if str(e["ts"]) >= iso(day_start)
    ]
    report.incidents = store.incidents(limit=20, since=day_start)
    report.open_positions = [p.to_dict(marks.get(p.symbol)) for p in open_positions]
    report.limits_hit = sorted(
        {
            ev["payload"].get("reason", "")
            for ev in events
            if ev["kind"] == "risk_veto" and ev["payload"].get("reason")
        }
    )[:8]
    report.cooldowns = [
        f"{ev['payload'].get('symbol')} until {ev['payload'].get('until', '')[:19]}"
        for ev in events
        if ev["kind"] == "cooldown"
    ]
    report.size_reductions = [
        f"{ev['payload'].get('strategy')} x{ev['payload'].get('multiplier')}"
        for ev in events
        if ev["kind"] == "size_reduction"
    ]
    report.system_health = dict(system_health or {})
    report.next_day = _next_day_state(store, config)
    return report


def _fmt_row(row: dict[str, Any]) -> dict[str, Any]:
    trades = row["trades"]
    return {
        "trades": trades,
        "wins": row["wins"],
        "pnl": to_str(row["pnl"], 2),
        "fees": to_str(row["fees"], 2),
        "win_rate": to_str(D(row["wins"]) / D(trades) if trades else ZERO, 3),
    }


def _next_day_state(store: Store, config: Config) -> list[str]:
    out: list[str] = []
    level = store.kill_level()
    if level != "L0":
        out.append(f"kill switch is {level} - entries stay closed until reset")
    paused = store.kv_get("paused_strategies", {}) or {}
    for name, info in paused.items():
        until = info.get("until") if isinstance(info, dict) else None
        if until:
            out.append(f"{name} paused until {until[:19]}")
        else:
            out.append(f"{name} disabled")
    for name in config.enabled_strategies:
        if name not in paused:
            out.append(f"{name} active")
    decays = store.kv_get("decayed_strategies", {}) or {}
    for name in decays:
        out.append(f"{name} auto-disabled by decay detector")
    return out


def write_reports(
    report: DailyReport,
    out_dir: Path | str,
    *,
    tax_ledger: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Write markdown, HTML, JSON and the tax ledger CSV. Returns the file paths."""
    day_dir = Path(out_dir) / report.date
    day_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": day_dir / "report.md",
        "html": day_dir / "report.html",
        "json": day_dir / "report.json",
        "telegram": day_dir / "telegram.txt",
        "trades_csv": day_dir / "trades.csv",
        "tax_csv": day_dir / "tax_ledger.csv",
    }
    paths["markdown"].write_text(report.markdown(), encoding="utf-8")
    paths["html"].write_text(report.html(), encoding="utf-8")
    import json

    paths["json"].write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    paths["telegram"].write_text(report.telegram(), encoding="utf-8")
    _write_csv(paths["trades_csv"], report.closed_trades, [
        "symbol", "strategy", "qty", "entry", "exit", "pnl", "fees", "opened_at", "closed_at",
        "exit_reason", "r_multiple",
    ])
    _write_csv(paths["tax_csv"], tax_ledger or [], [
        "date", "asset", "quantity", "cost", "sale_value", "fees", "gain",
    ])
    return {k: str(v) for k, v in paths.items()}


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c) for c in columns})


def tax_ledger_rows(closed_trades: list[dict[str, Any]], tds_pct: Decimal = D("0.01")) -> list[dict[str, Any]]:
    """Schedule VDA friendly rows: date, asset, quantity, cost, sale, fees, gain."""
    rows = []
    for trade in closed_trades:
        sale = D(trade.get("exit") or "0") * D(trade.get("qty") or "0")
        cost = D(trade.get("entry") or "0") * D(trade.get("qty") or "0")
        rows.append(
            {
                "date": str(trade.get("closed_at"))[:10],
                "asset": str(trade.get("symbol", "")).split("/")[0],
                "quantity": trade.get("qty"),
                "cost": to_str(cost, 2),
                "sale_value": to_str(sale, 2),
                "fees": trade.get("fees"),
                "gain": trade.get("pnl"),
                "tds_1pct": to_str(sale * tds_pct, 2),
            }
        )
    return rows


def day_bounds(now: datetime, boundary_hour: int = 0) -> tuple[datetime, datetime]:
    now = utc(now)
    start = now.replace(hour=boundary_hour, minute=0, second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=1)
    return start, start + timedelta(days=1)
