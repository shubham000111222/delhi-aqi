"""Dashboard must render exchange/log strings as text, not executable markup."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from tradebot.dashboard.app import TEMPLATE


def test_untrusted_dashboard_fields_are_html_escaped():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable for JavaScript rendering test")
    html = TEMPLATE.read_text(encoding="utf-8")
    script = html.split("<script>", 1)[1].split("async function tick()", 1)[0]
    test = r'''
const boxes = {};
globalThis.document = {getElementById: id => boxes[id] ||= {innerHTML: "", querySelector: () => boxes[id]}};
const bad = '<img src=x onerror=alert(1)>';
renderPositions([{symbol:bad,strategy:bad,qty:bad}]);
renderTrades([{symbol:bad,strategy:bad,exit_reason:bad}]);
renderSignals([{symbol:bad,side:bad,reason:bad}]);
renderVetoes({available:true, by_check:[{check:bad,count:bad}], note:bad});
renderFeed([{kind:bad,payload:{msg:bad}}]);
for (const [name, box] of Object.entries(boxes)) {
  if (box.innerHTML.includes('<img') || !box.innerHTML.includes('&lt;img')) throw Error(name);
}
renderRisk({limits:{max_drawdown:.15,daily_loss_flatten:.03,total_exposure:1,max_open_positions:5,
                    risk_per_trade:.01,daily_loss_stop:.02},
            status:{}, positions:[], equity:1000, day_pnl:0, day_start_equity:1000, drawdown_pct:-.2});
if (!boxes.risk.innerHTML.includes('-20.00% vs -15.00%')) throw Error('drawdown does not use peak');
'''
    subprocess.run([node, "-e", script + test], check=True, capture_output=True, text=True)
