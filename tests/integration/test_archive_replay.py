"""The archive replay must start where its data starts, and refuse unusable prices.

Both of these were real failures: a 2024 hard-coded start replayed a 2026 archive and
closed zero trades, and a NaN stop raised `decimal.InvalidOperation` in the middle of a
live session, which reads as a crash rather than as a rejected signal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tradebot.core.clock import SimClock
from tradebot.core.models import Candle, OrderType, Side, Signal
from tradebot.core.money import D
from tradebot.core.store import Store
from tradebot.engine import Engine
from tradebot.main import SIM_ORIGIN, _sim_end, _sim_start

ROOT = Path(__file__).resolve().parents[2]
START = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_caches():
    from tradebot import main as main_mod

    main_mod._ARCHIVE_BOUNDS.clear()
    yield
    main_mod._ARCHIVE_BOUNDS.clear()


class TestSimWindow:
    def test_synthetic_runs_keep_the_fixed_origin(self, fast_cfg):
        assert _sim_start(fast_cfg) == SIM_ORIGIN
        assert _sim_end(fast_cfg, SIM_ORIGIN, 3) == SIM_ORIGIN + timedelta(days=3)

    def test_an_archive_run_starts_on_the_first_bar(self, tmp_path):
        pytest.importorskip("pyarrow")
        pytest.importorskip("duckdb")
        from tradebot.config import load_config
        from tradebot.data.archive import Archive

        cfg = load_config(
            ROOT / "config",
            environ={},
            overrides={
                "data_dir": str(tmp_path / "data"),
                "report": {"dir": str(tmp_path / "reports")},
                "feed": "archive",
            },
        )
        cfg.ensure_dirs()
        first = datetime(2026, 3, 2, 6, 0, tzinfo=UTC)
        Archive(cfg.settings.archive_dir).write_candles(
            [
                Candle(
                    symbol="BTC/USDT",
                    timeframe="1m",
                    ts=first + timedelta(minutes=i),
                    open=D("100"),
                    high=D("101"),
                    low=D("99"),
                    close=D("100.5"),
                    volume=D("5"),
                )
                for i in range(10)
            ]
        )

        assert _sim_start(cfg) == first
        # a longer window than the tape stops at the tape, not in empty time
        assert _sim_end(cfg, first, 30) == first + timedelta(minutes=10)


class TestSignalSanity:
    @pytest.fixture
    async def engine(self, fast_cfg):
        fast_cfg.ensure_dirs()
        built = await Engine.build(fast_cfg, clock=SimClock(START), store=Store(":memory:"))
        yield built
        built.close()

    def _signal(self, stop) -> Signal:
        return Signal(
            id="sig-1",
            strategy="S1_ema_trend",
            symbol="BTC/USDT",
            timeframe="15m",
            side=Side.BUY,
            entry_type=OrderType.LIMIT,
            ts=START,
            stop=stop,
            target=D("120"),
            reason="test",
        )

    async def test_a_finite_signal_is_accepted(self, engine):
        assert engine._signal_is_sane(self._signal(D("95"))) is True

    @pytest.mark.parametrize("stop", [D("NaN"), D("Infinity"), D("-Infinity"), D("0"), D("-5")])
    async def test_an_unusable_stop_is_refused_and_logged(self, engine, stop):
        assert engine._signal_is_sane(self._signal(stop)) is False
        kinds = [row["kind"] for row in engine.store._query("select kind from events")]
        assert "signal_rejected" in kinds
