"""Render a dashboard snapshot to a PNG.

The dashboard is a web page, but the places a page is hardest to look at are exactly
the places a screenshot helps: a browser that will not render an embedded preview, an
incident channel that wants a picture, or an e-mail report. This draws the same
numbers straight from ``/api/snapshot`` with no browser involved.

Pillow is an optional dependency (``pip install -e ".[png]"``); importing this module
without it raises a clear error rather than breaking the dashboard.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

RGB = tuple[int, int, int]

BG: RGB = (11, 15, 20)
PANEL: RGB = (18, 24, 33)
LINE: RGB = (30, 41, 55)
TEXT: RGB = (230, 237, 245)
MUTED: RGB = (139, 155, 176)
GREEN: RGB = (47, 191, 113)
RED: RGB = (239, 83, 80)
AMBER: RGB = (245, 166, 35)
BLUE: RGB = (76, 154, 255)
CHIP: RGB = (27, 37, 50)

_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu/",
    "/usr/share/fonts/dejavu/",
    "/Library/Fonts/",
    "/System/Library/Fonts/Supplemental/",
)


def _require_pillow() -> Any:
    try:
        import PIL  # noqa: F401
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            'PNG export needs Pillow: pip install -e ".[png]"'
        ) from exc
    from PIL import Image, ImageDraw, ImageFont

    return Image, ImageDraw, ImageFont


def _font(size: int, *, bold: bool = False, mono: bool = False) -> Any:
    _, _, ImageFont = _require_pillow()
    stem = "DejaVuSansMono" if mono else "DejaVuSans"
    if bold:
        stem += "-Bold"
    for directory in _FONT_DIRS:
        candidate = Path(directory) / f"{stem}.ttf"
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def _money(value: Any, places: int = 2) -> str:
    try:
        return f"{float(value):,.{places}f}"
    except (TypeError, ValueError):
        return "–"


def _signed(value: Any, places: int = 2) -> str:
    try:
        return f"{float(value):+,.{places}f}"
    except (TypeError, ValueError):
        return "–"


def _curve_points(curve: Any) -> list[float]:
    points: list[float] = []
    for item in curve or []:
        try:
            points.append(float(item["equity"] if isinstance(item, dict) else item))
        except (TypeError, ValueError, KeyError):
            continue
    return points


def render_png(snapshot: dict[str, Any], out_path: str | Path) -> Path:
    """Draw ``snapshot`` (the /api/snapshot payload) to ``out_path`` and return it."""
    Image, ImageDraw, _ = _require_pillow()

    width, height = 1500, 1000
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)

    f_title = _font(20, bold=True)
    f_lbl = _font(12)
    f_val = _font(26, bold=True)
    f_body = _font(13)
    f_small = _font(11)
    f_head = _font(14, bold=True)

    def card(box: tuple[int, int, int, int]) -> None:
        draw.rounded_rectangle(box, 10, fill=PANEL, outline=LINE)

    def pill(x: int, y: int, text: str, colour: RGB) -> None:
        w = int(draw.textlength(text, font=f_small)) + 18
        draw.rounded_rectangle([x, y, x + w, y + 22], 11, fill=CHIP)
        draw.text((x + 9, y + 5), text, font=f_small, fill=colour)

    status = snapshot.get("status") or {}
    stats = status.get("stats") or {}
    kill = (snapshot.get("kill") or {}).get("level", "L0")

    # ---- header -------------------------------------------------------------
    draw.rectangle([0, 0, width, 62], fill=(14, 19, 26))
    draw.line([0, 62, width, 62], fill=LINE)
    draw.text((24, 20), "tradebot", font=f_title, fill=TEXT)
    # below the wordmark, not beside it: "tradebot" at 20pt bold is wider than 84px and
    # the subtitle used to overlap it
    draw.text((130, 25), "paper trading dashboard", font=f_body, fill=MUTED)

    equity = float(snapshot.get("equity") or 0)
    day_pnl = float(snapshot.get("day_pnl") or 0)
    day_pct = float(snapshot.get("day_pnl_pct") or 0) * 100

    x = 430
    draw.text((x, 24), "EQUITY", font=f_lbl, fill=MUTED)
    draw.text((x, 38), _money(equity), font=_font(16, bold=True), fill=GREEN if equity >= 1000 else RED)
    x += 150
    draw.text((x, 24), "DAY P&L", font=f_lbl, fill=MUTED)
    draw.text(
        (x, 38),
        f"{_signed(day_pnl)} ({day_pct:+.2f}%)",
        font=_font(16, bold=True),
        fill=GREEN if day_pnl >= 0 else RED,
    )
    x += 230
    draw.text((x, 24), "KILL SWITCH", font=f_lbl, fill=MUTED)
    pill(x, 36, kill, GREEN if kill == "L0" else AMBER if kill == "L1" else RED)
    x += 130
    draw.text((x, 24), "MODE", font=f_lbl, fill=MUTED)
    pill(x, 36, str(status.get("mode", "paper")), BLUE)
    x += 110
    draw.text((x, 24), "MARKET CLOCK", font=f_lbl, fill=MUTED)
    draw.text(
        (x, 38),
        str(status.get("now", ""))[:16].replace("T", " "),
        font=_font(14, bold=True),
        fill=TEXT,
    )

    # ---- metrics ------------------------------------------------------------
    metrics = [
        ("REALIZED P&L", _signed(status.get("realized_pnl", 0))),
        ("UNREALIZED", _signed(status.get("unrealized_pnl", 0))),
        ("CASH", _money(status.get("cash", 0))),
        ("OPEN POSITIONS", str(len(snapshot.get("positions") or []))),
        ("TRADES CLOSED", str(stats.get("trades_closed", 0))),
        ("FEES PAID", _money(status.get("fees", 0))),
    ]
    box_w = (width - 48 - 5 * 12) // 6
    for i, (label, value) in enumerate(metrics):
        x0 = 24 + i * (box_w + 12)
        card((x0, 84, x0 + box_w, 162))
        draw.text((x0 + 16, 98), label, font=f_lbl, fill=MUTED)
        colour = GREEN if value.startswith("+") else RED if value.startswith("-") else TEXT
        draw.text((x0 + 16, 124), value, font=f_val, fill=colour)

    # ---- equity curve -------------------------------------------------------
    curve_top = 178
    card((24, curve_top, width - 24, curve_top + 268))
    draw.text((44, curve_top + 18), "EQUITY CURVE", font=f_head, fill=TEXT)
    points = _curve_points(snapshot.get("equity_curve"))
    draw.text(
        (184, curve_top + 20),
        f"{len(points)} snapshots · simulated market minutes",
        font=f_small,
        fill=MUTED,
    )
    if len(points) >= 2:
        lo = min(min(points), 1000.0)
        hi = max(max(points), 1000.0)
        span = (hi - lo) or 1.0
        px0, px1 = 44, width - 60
        py0, py1 = curve_top + 60, curve_top + 270
        coords = [
            (
                px0 + i * (px1 - px0) / (len(points) - 1),
                py1 - (value - lo) / span * (py1 - py0),
            )
            for i, value in enumerate(points)
        ]
        base = py1 - (1000.0 - lo) / span * (py1 - py0)
        draw.line([(px0, base), (px1, base)], fill=(60, 74, 92), width=1)
        draw.text((px1 - 108, base - 16), "start 1,000", font=f_small, fill=MUTED)
        for k in range(1, 5):
            yk = py0 + k * (py1 - py0) / 5
            draw.line([(px0, yk), (px1, yk)], fill=(23, 31, 42), width=1)
        draw.polygon([*coords, (px1, py1), (px0, py1)], fill=(17, 46, 36))
        draw.line(coords, fill=GREEN, width=2)
        draw.text((44, curve_top + 40), f"high {_money(hi)}", font=f_small, fill=MUTED)
        draw.text((44, py1 - 14), f"low  {_money(lo)}", font=f_small, fill=MUTED)
    else:
        draw.text((44, curve_top + 70), "warming up", font=f_body, fill=MUTED)

    # ---- execution cost -----------------------------------------------------
    cost_top = 458
    card((24, cost_top, width - 24, cost_top + 44))
    draw.text((44, cost_top + 15), "EXECUTION COST", font=f_head, fill=TEXT)
    draw.text((186, cost_top + 17), "slippage per fill vs the price the decision was made at",
              font=f_small, fill=MUTED)
    slip = snapshot.get("slippage") or {}
    if not slip.get("available"):
        draw.text((600, cost_top + 17), str(slip.get("note") or "no fills with a decision price yet"),
                  font=f_small, fill=MUTED)
    else:
        budget = float(slip.get("budget_bps") or 0)
        p95 = float(slip.get("p95_bps") or 0)
        model = slip.get("sizing_model") or {}
        cells = [
            ("FILLS", str(slip.get("fills", 0)), TEXT),
            ("MEDIAN", f"{float(slip.get('median_bps') or 0):+.2f} bps", TEXT),
            ("MEAN", f"{float(slip.get('mean_bps') or 0):+.2f} bps", TEXT),
            ("P95", f"{p95:+.2f} / {budget:.0f} bps", RED if p95 > budget else GREEN),
            ("WORST", f"{float(slip.get('worst_bps') or 0):+.2f} bps", TEXT),
            ("SIZE CAP", f"{model.get('samples')} fills fitted" if model else "not fitted", MUTED),
        ]
        x0 = 600
        for label, value, colour in cells:
            draw.text((x0, cost_top + 8), label, font=f_lbl, fill=MUTED)
            draw.text((x0, cost_top + 24), value, font=f_small, fill=colour)
            x0 += 150

    # ---- refusals -----------------------------------------------------------
    veto_top = 510
    card((24, veto_top, width - 24, veto_top + 44))
    draw.text((44, veto_top + 15), "WHY ENTRIES WERE REFUSED", font=f_head, fill=TEXT)
    draw.text((292, veto_top + 17), "top risk checks over the recent verdict log", font=f_small, fill=MUTED)
    veto = snapshot.get("veto_breakdown") or {}
    if not veto.get("available"):
        draw.text((600, veto_top + 17), str(veto.get("note") or "no risk verdicts logged yet"),
                  font=f_small, fill=MUTED)
    else:
        draw.text(
            (600, veto_top + 8),
            f"{veto.get('refused', 0)} refused / {veto.get('window', 0)} verdicts",
            font=f_lbl,
            fill=MUTED,
        )
        draw.text(
            (600, veto_top + 24),
            f"{float(veto.get('approval_rate') or 0) * 100:.1f}% of entries approved",
            font=f_small,
            fill=GREEN if float(veto.get("approval_rate") or 0) > 0.05 else AMBER,
        )
        if veto.get("note"):
            draw.text((44, veto_top + 32), str(veto["note"])[:120], font=f_small, fill=MUTED)
        x0 = 860
        for row in (veto.get("by_check") or [])[:4]:
            colour = AMBER if row.get("check") == "cost_budget" else TEXT
            draw.text((x0, veto_top + 8), str(row.get("check", ""))[:16], font=f_lbl, fill=MUTED)
            draw.text((x0, veto_top + 24), f"{row.get('count', 0)}", font=f_small, fill=colour)
            x0 += 150

    # ---- positions ----------------------------------------------------------
    table_top = 568
    card((24, table_top, 730, height - 24))
    draw.text((44, table_top + 16), "OPEN POSITIONS", font=f_head, fill=TEXT)
    for name, xcol in (
        ("SYMBOL", 44),
        ("STRATEGY", 190),
        ("QTY", 360),
        ("ENTRY", 470),
        ("MARK", 570),
        ("R", 670),
    ):
        draw.text((xcol, table_top + 52), name, font=f_lbl, fill=MUTED)
    draw.line([40, table_top + 72, 714, table_top + 72], fill=LINE)
    positions = snapshot.get("positions") or []
    if not positions:
        draw.text((44, table_top + 90), "flat — no open positions", font=f_body, fill=MUTED)
    for i, pos in enumerate(positions[:9]):
        y = table_top + 84 + i * 30
        draw.text((44, y), str(pos.get("symbol", ""))[:12], font=f_body, fill=TEXT)
        draw.text((190, y), str(pos.get("strategy", ""))[:16], font=f_body, fill=MUTED)
        draw.text((360, y), _money(pos.get("qty"), 4), font=f_body, fill=TEXT)
        draw.text((470, y), _money(pos.get("avg_entry")), font=f_body, fill=TEXT)
        draw.text((570, y), _money(pos.get("mark")), font=f_body, fill=TEXT)
        r_value = float(pos.get("r_multiple") or 0)
        draw.text((670, y), f"{r_value:+.2f}", font=f_body, fill=GREEN if r_value >= 0 else RED)

    # ---- trades -------------------------------------------------------------
    card((746, table_top, width - 24, height - 24))
    draw.text((766, table_top + 16), "RECENT TRADES", font=f_head, fill=TEXT)
    for name, xcol in (("SYMBOL", 766), ("EXIT", 900), ("PNL", 1080), ("REASON", 1200)):
        draw.text((xcol, table_top + 52), name, font=f_lbl, fill=MUTED)
    draw.line([762, table_top + 72, width - 40, table_top + 72], fill=LINE)
    trades = snapshot.get("trades") or []
    if not trades:
        draw.text((766, table_top + 90), "no closed trades yet", font=f_body, fill=MUTED)
    for i, trade in enumerate(list(reversed(trades))[:9]):
        y = table_top + 84 + i * 30
        pnl = float(trade.get("pnl") or 0)
        draw.text((766, y), str(trade.get("symbol", ""))[:10], font=f_body, fill=TEXT)
        draw.text((900, y), _money(trade.get("exit_price")), font=f_body, fill=TEXT)
        draw.text((1080, y), _signed(pnl), font=f_body, fill=GREEN if pnl >= 0 else RED)
        draw.text((1200, y), str(trade.get("exit_reason", ""))[:14], font=f_body, fill=MUTED)

    # ---- footer -------------------------------------------------------------
    footer = (
        f"signals {stats.get('signals', 0)} · vetoes {stats.get('vetoes', 0)} · "
        f"orders {stats.get('orders', 0)} · fills {stats.get('fills', 0)} · "
        f"rendered {datetime.now().strftime('%Y-%m-%d %H:%M')} from /api/snapshot"
    )
    draw.text((24, height - 18), footer, font=f_small, fill=MUTED)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, "PNG", optimize=True)
    return out
