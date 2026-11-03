from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tradebot.config import Config, load_config
from tradebot.core.clock import SimClock
from tradebot.core.models import BookLevel, MarketRules, OrderBook
from tradebot.core.money import D

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = load_config(
        ROOT / "config",
        environ={},
        overrides={
            "data_dir": str(tmp_path / "data"),
            "report": {"dir": str(tmp_path / "reports")},
        },
    )
    config.ensure_dirs()
    return config


@pytest.fixture
def fast_cfg(tmp_path: Path) -> Config:
    """A two-symbol, 15m-only config so engine tests stay fast."""
    import shutil

    src = ROOT / "config"
    dst = tmp_path / "config"
    shutil.copytree(src, dst)
    (dst / "symbols.yaml").write_text(
        """
symbols:
  - symbol: BTC/USDT
    enabled: true
    strategy_timeframes: ["15m"]
    tick_size: "0.1"
    lot_step: "0.00001"
    min_qty: "0.00001"
    min_notional: "5"
  - symbol: ETH/USDT
    enabled: true
    strategy_timeframes: ["15m"]
    tick_size: "0.01"
    lot_step: "0.0001"
    min_qty: "0.0001"
    min_notional: "5"
""",
        encoding="utf-8",
    )
    # strategies keep 1h declarations: the loader validates them against derived TFs,
    # so we derive 15m and 1h but the engine only sees 15m bars closing (fast test).
    config = load_config(
        dst,
        environ={},
        overrides={
            "data_dir": str(tmp_path / "data"),
            "strategy_timeframes": ["15m", "1h"],
            "report": {"dir": str(tmp_path / "reports")},
            "synthetic": {"seed": 11, "speed": 60, "start_ts": "2024-01-01T00:00:00Z", "equity_regime": "mixed"},
        },
    )
    for cfg_name, strat in config.strategies.strategies.items():
        if "1h" in strat.timeframes:
            config.strategies.strategies[cfg_name] = strat.model_copy(
                update={"timeframes": tuple(tf for tf in strat.timeframes if tf != "1h") or ("15m",)}
            )
    config.ensure_dirs()
    return config


@pytest.fixture
def sim_clock() -> SimClock:
    return SimClock(datetime(2024, 1, 1, tzinfo=UTC))


@pytest.fixture
def rules() -> dict[str, MarketRules]:
    return {
        "BTC/USDT": MarketRules(
            symbol="BTC/USDT",
            tick_size=D("0.1"),
            lot_step=D("0.00001"),
            min_qty=D("0.00001"),
            min_notional=D("5"),
            maker_fee=D("0.001"),
            taker_fee=D("0.001"),
        )
    }


def make_book(
    symbol: str = "BTC/USDT",
    mid: str = "60000",
    levels: int = 20,
    qty_per_level: str = "0.5",
    spread_ticks: int = 2,
    tick: str = "0.1",
    ts: datetime | None = None,
) -> OrderBook:
    m = D(mid)
    t = D(tick)
    half = t * spread_ticks / 2
    bids = tuple(BookLevel(price=m - half - t * i, qty=D(qty_per_level)) for i in range(levels))
    asks = tuple(BookLevel(price=m + half + t * i, qty=D(qty_per_level)) for i in range(levels))
    return OrderBook(symbol=symbol, ts=ts or datetime(2024, 1, 1, tzinfo=UTC), bids=bids, asks=asks)


def make_candles(
    symbol: str = "BTC/USDT",
    start: str = "2024-01-01T00:00:00Z",
    count: int = 200,
    start_price: float = 60000.0,
    step: float = 10.0,
    timeframe: str = "1m",
) -> list:
    from tradebot.core.models import Candle

    ts0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    out = []
    price = start_price
    for i in range(count):
        o = price
        c = price + step
        out.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                ts=ts0 + timedelta(minutes=i),
                open=D(o),
                high=D(max(o, c) + 5),
                low=D(min(o, c) - 5),
                close=D(c),
                volume=D(100 + i % 17),
            )
        )
        price = c
    return out


@pytest.fixture
def book():
    return make_book()


@pytest.fixture
def candles():
    return make_candles()
