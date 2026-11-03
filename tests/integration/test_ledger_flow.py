"""`tradebot learn` against a real store: does the CLI promote what deserves it, and only that?

The unit tests prove the maths. This one proves the wiring: a store full of trades the
engine actually wrote, a CLI invocation, a card on disk, and a filter that refuses the
setup the ledger lost on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tradebot import main as cli
from tradebot.ai.ledger import LedgerEntryFilter, LedgerGate, Vocabulary, load_card
from tradebot.core.models import Position
from tradebot.core.store import Store

START = datetime(2024, 3, 1, tzinfo=UTC)


def _write_trade(
    store: Store,
    index: int,
    *,
    win: bool,
    atr: Decimal,
    strategy: str = "S1_ema_trend",
    symbol: str = "BTC/USDT",
) -> None:
    """One position, opened a day apart, closed four hours later."""
    entry, stop = Decimal("100"), Decimal("98")
    position = Position(
        symbol=symbol,
        strategy=strategy,
        qty=Decimal("1"),
        avg_entry=entry,
        stop=stop,
        target=Decimal("104"),
        initial_stop=stop,
        opened_at=START + timedelta(days=index),
        atr_at_entry=atr,
        risk_per_unit=Decimal("2"),
        initial_qty=Decimal("1"),
    )
    position.realized_pnl = Decimal("4") if win else Decimal("-2")
    position.fees = Decimal("0.2")
    store.save_position(position)
    store.close_position(
        position,
        exit_price=Decimal("104") if win else Decimal("98"),
        exit_reason="target" if win else "stop",
        closed_at=START + timedelta(days=index, hours=4),
    )


def seed(path: Path, *, learnable: bool, count: int = 330) -> Path:
    store = Store(str(path))
    for index in range(count):
        if learnable:
            high_vol = index % 2 == 0
            _write_trade(store, index, win=high_vol, atr=Decimal("0.8") if high_vol else Decimal("0.2"))
        else:
            # outcome alternates, volatility is constant: nothing to condition on
            _write_trade(store, index, win=index % 2 == 0, atr=Decimal("0.5"))
    store.close()
    return path


def _stub_config(db_path: Path) -> Any:
    return SimpleNamespace(
        settings=SimpleNamespace(db_path=str(db_path), mode=SimpleNamespace(value="paper")),
        mode=SimpleNamespace(value="paper"),
    )


class TestLearnCommand:
    def test_it_promotes_a_learnable_ledger_and_writes_a_card(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = seed(tmp_path / "paper.db", learnable=True)
        monkeypatch.setattr(cli, "_config", lambda args: _stub_config(db))
        card = tmp_path / "models" / "ledger.json"

        code = cli.main(["learn", "--save", str(card), "--json"])

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"]["status"] == "PROMOTABLE"
        assert payload["ledger"]["trades"] == 330
        assert payload["card"] == str(card)
        assert card.exists()

        model = load_card(card)
        assert model.ready
        assert model.meta["verdict"]["status"] == "PROMOTABLE"

    def test_the_card_it_writes_refuses_the_setups_that_lost(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = seed(tmp_path / "paper.db", learnable=True)
        monkeypatch.setattr(cli, "_config", lambda args: _stub_config(db))
        card = tmp_path / "ledger.json"
        assert cli.main(["learn", "--save", str(card)]) == 0

        entry_filter = LedgerEntryFilter(gate=load_card(card))
        signal = SimpleNamespace(
            strategy="S1_ema_trend", symbol="BTC/USDT", stop=Decimal("98"),
            target=Decimal("104"), ts=START, confidence=Decimal("0.5"),
        )

        allowed_low, _ = entry_filter(signal, SimpleNamespace(mark=Decimal("100"), atr=0.2))
        allowed_high, _ = entry_filter(signal, SimpleNamespace(mark=Decimal("100"), atr=0.8))

        assert allowed_low is False  # 20 bps of ATR: the ledger lost on these
        assert allowed_high is True  # 80 bps: the ledger won on these
        assert entry_filter.vetoes == 1

    def test_noise_is_refused_and_no_card_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = seed(tmp_path / "paper.db", learnable=False)
        monkeypatch.setattr(cli, "_config", lambda args: _stub_config(db))
        card = tmp_path / "ledger.json"

        assert cli.main(["learn", "--save", str(card), "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"]["status"] == "NO_EDGE"
        assert payload["card"] is None
        assert not card.exists()

    def test_a_small_ledger_says_not_yet_rather_than_guessing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = seed(tmp_path / "paper.db", learnable=True, count=40)
        monkeypatch.setattr(cli, "_config", lambda args: _stub_config(db))
        monkeypatch.setattr(cli, "_print", lambda obj: print(json.dumps(obj, default=str)))

        assert cli.main(["learn", "--json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"]["status"] == "NEED_MORE_DATA"
        assert "200 required" in " ".join(payload["verdict"]["reasons"])


class TestLedgerGateAtRuntime:
    def test_run_refuses_a_card_of_the_wrong_kind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        card = tmp_path / "meta.json"
        card.write_text(json.dumps({"kind": "meta_label"}))
        closed: list[bool] = []
        engine_stub = SimpleNamespace(entry_filter=None, close=lambda: closed.append(True))
        monkeypatch.setattr(cli, "_config", lambda args: _stub_config(tmp_path / "x.db"))
        monkeypatch.setattr(cli, "_build_engine", lambda cfg: _resolved(engine_stub))

        assert cli.main(["run", "--ledger-gate", str(card)]) == 2
        assert closed == [True]
        assert "not a ledger-gate card" in capsys.readouterr().err

    def test_two_filters_can_only_remove_more_trades(self) -> None:
        calls: list[str] = []

        def first(signal: Any, ctx: Any = None) -> tuple[bool, str]:
            calls.append("first")
            return False, "first said no"

        def second(signal: Any, ctx: Any = None) -> tuple[bool, str]:
            calls.append("second")
            return True, "second said yes"

        chained = cli._chain_filters(first, second)

        assert chained(None) == (False, "first said no")
        assert calls == ["first"]  # the second filter is not even consulted


async def _resolved(value: Any) -> Any:
    return value


class TestLedgerGateInsideTheEngine:
    async def test_a_card_that_refuses_everything_stops_entries_at_the_hook(self, fast_cfg) -> None:
        """End to end through the real engine: signals happen, orders do not.

        The gate is fitted to refuse every (strategy, symbol) the config can produce, so
        the assertion is not "fewer trades" - it is "the hook is actually inline between a
        signal and the order book", which is the only thing worth proving here.
        """
        import numpy as np

        from tradebot.ai.meta import LogisticGate
        from tradebot.core.clock import SimClock
        from tradebot.engine import Engine

        fast_cfg.ensure_dirs()
        engine = await Engine.build(fast_cfg, clock=SimClock(START), store=Store(":memory:"))
        try:
            vocabulary = Vocabulary(
                strategies=sorted(engine.strategies), symbols=fast_cfg.symbol_names()
            )
            size = len(vocabulary.names())
            model = LogisticGate()
            model.coef_ = np.zeros(size + 1)
            model.mean_ = np.zeros(size)
            model.scale_ = np.ones(size)
            gate = LedgerGate(model=model, vocabulary=vocabulary, threshold=1.01)
            entry_filter = LedgerEntryFilter(gate=gate)
            engine.entry_filter = entry_filter

            await engine.run(stop_at=START + timedelta(days=2))

            assert engine.stats.signals > 0, "the strategies never fired; nothing was tested"
            assert entry_filter.vetoes > 0, "the filter was never consulted"
            assert engine.stats.orders == 0, "an order escaped a card that refuses everything"
            verdicts = engine.store.verdicts(limit=2000)
            vetoed = [row for row in verdicts if not row["approved"]]
            assert any("entry_filter" in (row["reason"] or "") for row in vetoed)
        finally:
            engine.close()


def test_every_attempt_is_written_to_the_trial_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An attempt that is not logged is an attempt nobody can deflate later."""
    db = seed(tmp_path / "paper.db", learnable=False)
    monkeypatch.setattr(cli, "_config", lambda args: _stub_config(db))

    assert cli.main(["learn", "--json"]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]

    store = Store(str(db))
    try:
        rows = store.experiments(limit=10)
    finally:
        store.close()

    assert len(rows) == 1
    assert rows[0]["run_id"] == run_id
    assert rows[0]["kind"] == "ledger_gate"
    assert rows[0]["notes"] == "NO_EDGE"
    assert rows[0]["metrics"]["trades"] == 330


class TestInspect:
    def _card(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        db = seed(tmp_path / "paper.db", learnable=True)
        monkeypatch.setattr(cli, "_config", lambda args: _stub_config(db))
        card = tmp_path / "ledger.json"
        assert cli.main(["learn", "--save", str(card)]) == 0
        return card

    def test_a_fresh_card_is_reported_valid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        card = self._card(tmp_path, monkeypatch)

        assert cli.main(["learn", "--inspect", str(card)]) == 0
        out = capsys.readouterr().out
        assert "verdict PROMOTABLE" in out
        assert "valid" in out

    def test_an_expired_card_is_reported_inert(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        card = self._card(tmp_path, monkeypatch)
        payload = json.loads(card.read_text())
        payload["created_at"] = "2020-01-01T00:00:00+00:00"
        card.write_text(json.dumps(payload))

        assert cli.main(["learn", "--inspect", str(card)]) == 3
        assert "INERT" in capsys.readouterr().out

    def test_a_file_that_is_not_a_card_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_config", lambda args: _stub_config(tmp_path / "x.db"))
        broken = tmp_path / "broken.json"
        broken.write_text('{"kind": "something_else"}')

        assert cli.main(["learn", "--inspect", str(broken)]) == 2
        assert "not a ledger-gate card" in capsys.readouterr().err
