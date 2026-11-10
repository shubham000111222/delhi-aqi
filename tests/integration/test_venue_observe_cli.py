"""The venue observation CLI does not trade or certify a recovery."""
import json

from tradebot import main as cli
from tradebot.execution.adapters.ccxt_live import CcxtLiveAdapter


class FakeVenue:
    has = {"fetchBalance": True, "fetchOpenOrders": True}

    def __init__(self):
        self.calls = []

    async def fetch_balance(self):
        self.calls.append("balance")
        return {"total": {"USDT": 20}, "free": {"USDT": 20}, "used": {"USDT": 0}}

    async def fetch_open_orders(self):
        self.calls.append("all-orders")
        return []


async def nothing(self):
    self.exchange = FAKE


async def close(self):
    self.exchange.calls.append("close")


FAKE = FakeVenue()


def test_venue_observe_cli_uses_read_only_calls_and_prints_caveat(fast_cfg, capsys, monkeypatch):
    FAKE.calls.clear()
    monkeypatch.setattr(CcxtLiveAdapter, "connect", nothing)
    monkeypatch.setattr(CcxtLiveAdapter, "close", close)
    assert cli.main(["venue-observe", "--config-dir", str(fast_cfg.settings.config_dir), "--mode", "testnet"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "testnet"
    assert output["balances"]["USDT"]["total"] == "20"
    assert output["usable_for_reset"] is False and output["orders_complete"] is False
    assert FAKE.calls == ["balance", "all-orders", "close"]


def test_venue_observe_rejects_paper_before_connect(fast_cfg, capsys, monkeypatch):
    FAKE.calls.clear()
    monkeypatch.setattr(CcxtLiveAdapter, "connect", nothing)
    assert cli.main(["venue-observe", "--config-dir", str(fast_cfg.settings.config_dir), "--mode", "paper"]) == 2
    assert "requires" in capsys.readouterr().err
    assert FAKE.calls == []
