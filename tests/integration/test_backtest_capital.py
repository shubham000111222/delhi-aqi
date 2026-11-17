"""Cash carried into a simulation must actually reach both sides of the ledger."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradebot.backtest.walkforward import run_segment
from tradebot.main import _run_backtest


async def test_walk_forward_segment_preserves_carried_capital_without_trades(fast_cfg):
    start = datetime(2024, 1, 1, tzinfo=UTC)
    result = await run_segment(fast_cfg, start, start + timedelta(minutes=1), capital=Decimal("1200"), seed=7)
    assert not result.trades
    assert result.start_equity == Decimal("1200")
    assert result.end_equity == Decimal("1200")


async def test_backtest_preserves_custom_starting_cash_without_trades(fast_cfg):
    result = await _run_backtest(fast_cfg, days=0, capital=Decimal("1200"), seed=7)
    assert result["start_equity"] == "1200.00"
    assert result["end_equity"] == "1200.00"
    assert result["closed_trades"] == 0
