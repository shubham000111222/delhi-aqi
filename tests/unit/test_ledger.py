"""Learning from the bot's own ledger: labels, purge, windows, verdict, and the filter.

The interesting cases are the negative ones. A gate that promotes itself on noise, or a
filter that quietly stops trading, is worse than no gate at all - so the tests that matter
most here are "no edge" and "fails open".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradebot.ai.ledger import (
    MIN_TRADES,
    VERDICT_NEED_DATA,
    VERDICT_NO_EDGE,
    VERDICT_PROMOTABLE,
    LedgerEntryFilter,
    LedgerGate,
    LedgerReport,
    LedgerTrade,
    Vocabulary,
    fit_gate,
    judge,
    load_card,
    save_card,
    trades_from_store,
    walk_forward,
)
from tradebot.core.models import Position
from tradebot.core.store import Store

START = datetime(2024, 3, 1, tzinfo=UTC)


def make_trade(
    index: int,
    *,
    strategy: str = "S1_ema_trend",
    symbol: str = "BTC/USDT",
    win: bool = True,
    atr_bps: float = 30.0,
    net_r: float | None = None,
    fees: float = 1.0,
) -> LedgerTrade:
    """A trade at hour `index % 24`, one day apart, with an explicit outcome."""
    opened = START + timedelta(days=index)
    return LedgerTrade(
        strategy=strategy,
        symbol=symbol,
        opened_at=opened,
        closed_at=opened + timedelta(hours=4),
        net_pnl=(net_r if net_r is not None else (1.0 if win else -1.0)) * 10.0,
        risk_amount=10.0,
        fees=fees,
        net_r=net_r if net_r is not None else (1.0 if win else -1.0),
        stress_r=(net_r if net_r is not None else (1.0 if win else -1.0)) - fees / 10.0,
        features={"atr_bps": atr_bps, "stop_bps": 100.0, "reward_bps": 200.0, "rr": 2.0},
    )


def planted_edge(count: int = 300) -> list[LedgerTrade]:
    """A ledger where high ATR *at entry* predicts the winner. Learnable, deterministic."""
    trades = []
    for index in range(count):
        high_atr = index % 2 == 0
        trades.append(
            make_trade(
                index,
                win=high_atr,
                atr_bps=80.0 if high_atr else 20.0,
                net_r=1.0 if high_atr else -1.0,
            )
        )
    return trades


# ---------------------------------------------------------------------------
# reading the ledger
# ---------------------------------------------------------------------------


def _store_with_trades(path: Path, rows: int = 3) -> Store:
    store = Store(str(path))
    for index in range(rows):
        entry = Decimal("100")
        stop = Decimal("98")
        position = Position(
            symbol="BTC/USDT",
            strategy="S1_ema_trend",
            qty=Decimal("1"),
            avg_entry=entry,
            stop=stop,
            target=Decimal("104"),
            initial_stop=stop,
            opened_at=START + timedelta(days=index),
            atr_at_entry=Decimal("0.9"),
            risk_per_unit=Decimal("2"),
            initial_qty=Decimal("1"),
        )
        position.realized_pnl = Decimal("4") if index % 2 == 0 else Decimal("-2")
        position.fees = Decimal("0.5")
        store.save_position(position)
        store.close_position(
            position, exit_price=Decimal("104") if index % 2 == 0 else Decimal("98"),
            exit_reason="target" if index % 2 == 0 else "stop",
            closed_at=START + timedelta(days=index, hours=3),
        )
    return store


class TestLedgerParsing:
    def test_labels_are_net_r_and_re_stressed(self, tmp_path: Path) -> None:
        store = _store_with_trades(tmp_path / "t.db")
        report = trades_from_store(store)
        store.close()

        assert len(report.trades) == 3
        winner = report.trades[0] if report.trades[0].net_r > 0 else report.trades[1]
        assert winner.net_r == pytest.approx(4 / 2)  # P&L / risk
        assert winner.stress_r == pytest.approx((4 - 0.5) / 2)  # fees charged twice
        assert winner.features["stop_bps"] == pytest.approx(200.0)  # 2 of 100
        assert winner.features["atr_bps"] == pytest.approx(90.0)

    def test_a_trade_without_a_stop_is_skipped_not_imputed(self, tmp_path: Path) -> None:
        store = _store_with_trades(tmp_path / "t.db")
        position = Position(
            symbol="ETH/USDT",
            strategy="S1_ema_trend",
            qty=Decimal("1"),
            avg_entry=Decimal("100"),
            stop=Decimal("100"),  # no risk: no R multiple can exist
            target=Decimal("104"),
            initial_stop=Decimal("100"),
            opened_at=START,
            risk_per_unit=Decimal("0"),
            initial_qty=Decimal("1"),
        )
        store.save_position(position)
        store.close_position(position, exit_price=Decimal("101"), exit_reason="target")
        report = trades_from_store(store)
        store.close()

        assert len(report.trades) == 3
        assert report.skipped == 1
        assert "no risk amount" in next(iter(report.skip_reasons))

    def test_summary_reports_the_fee_share(self) -> None:
        report = LedgerReport(trades=[make_trade(i, win=i % 2 == 0) for i in range(10)])
        summary = report.summary()

        assert summary["trades"] == 10
        assert summary["win_rate"] == pytest.approx(0.5)
        assert 0 <= summary["fee_share_of_gross"] <= 9.99


# ---------------------------------------------------------------------------
# the purge
# ---------------------------------------------------------------------------


class TestPurge:
    def test_a_trade_that_closed_after_the_window_opened_is_not_training_data(self) -> None:
        trades = planted_edge(240)
        windows = walk_forward(trades, windows=2)
        assert windows

        for window in windows:
            resolved_before = [trade for trade in trades if trade.closed_at < window.start]
            # nothing that was still open when the window began may be in its training set
            leaked = [trade for trade in resolved_before if trade.closed_at >= window.start]
            assert not leaked
            assert window.n_train <= len(resolved_before)

    def test_every_window_tests_on_later_trades_than_it_trained_on(self) -> None:
        windows = walk_forward(planted_edge(240), windows=3)
        assert len(windows) == 3
        for window in windows:
            assert window.n_train >= 40
            assert window.n_test >= 20
            assert window.start <= window.end


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------


class TestVerdict:
    def test_too_few_trades_is_a_data_problem_not_a_modelling_one(self) -> None:
        report = LedgerReport(trades=planted_edge(31))
        verdict = judge(report, walk_forward(report.trades))

        assert verdict.status == VERDICT_NEED_DATA
        assert f"{MIN_TRADES} required" in " ".join(verdict.reasons)
        assert not verdict.may_write_card

    def test_a_planted_edge_is_promotable(self) -> None:
        report = LedgerReport(trades=planted_edge(300))
        windows = walk_forward(report.trades, windows=3)
        verdict = judge(report, windows)

        assert verdict.status == VERDICT_PROMOTABLE, verdict.reasons
        assert all(window.auc > 0.9 for window in windows)
        assert all(window.gated_r > window.base_r for window in windows)

    def test_noise_is_refused(self) -> None:
        """Alternating outcomes with no feature relationship: nothing to learn."""
        trades = [make_trade(i, win=i % 2 == 0, atr_bps=30.0) for i in range(300)]
        report = LedgerReport(trades=trades)
        verdict = judge(report, walk_forward(trades, windows=3))

        assert verdict.status == VERDICT_NO_EDGE
        joined = " ".join(verdict.reasons)
        assert "AUC" in joined or "10%" in joined
        assert not verdict.may_write_card

    def test_a_fee_eaten_edge_is_refused_even_when_it_ranks(self) -> None:
        trades = planted_edge(300)
        # same ranking, but the fees now exceed the expectancy of the gated subset
        strained = [
            LedgerTrade(
                strategy=trade.strategy,
                symbol=trade.symbol,
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
                net_pnl=trade.net_pnl,
                risk_amount=trade.risk_amount,
                fees=trade.fees,
                net_r=trade.net_r,
                stress_r=-0.2,
                features=trade.features,
            )
            for trade in trades
        ]
        report = LedgerReport(trades=strained)
        verdict = judge(report, walk_forward(strained, windows=3))

        assert verdict.status == VERDICT_NO_EDGE
        assert any("2x costs" in reason for reason in verdict.reasons)


# ---------------------------------------------------------------------------
# the card and the runtime filter
# ---------------------------------------------------------------------------


class TestCard:
    def test_a_card_is_refused_unless_the_verdict_promoted(self, tmp_path: Path) -> None:
        report = LedgerReport(trades=planted_edge(60))
        verdict = judge(report, walk_forward(report.trades))
        gate = fit_gate(report.trades)

        with pytest.raises(ValueError, match="refusing to write"):
            save_card(tmp_path / "card.json", gate, verdict, report=report)
        assert not (tmp_path / "card.json").exists()

    def test_a_promoted_card_round_trips(self, tmp_path: Path) -> None:
        report = LedgerReport(trades=planted_edge(300))
        verdict = judge(report, walk_forward(report.trades, windows=3))
        gate = fit_gate(report.trades)
        path = save_card(tmp_path / "card.json", gate, verdict, report=report)

        loaded = load_card(path)
        high = loaded.decide(
            {"atr_bps": 80.0, "stop_bps": 100.0, "reward_bps": 200.0, "rr": 2.0},
            strategy="S1_ema_trend",
            symbol="BTC/USDT",
            moment=START,
        )
        low = loaded.decide(
            {"atr_bps": 20.0, "stop_bps": 100.0, "reward_bps": 200.0, "rr": 2.0},
            strategy="S1_ema_trend",
            symbol="BTC/USDT",
            moment=START,
        )

        assert loaded.threshold == gate.threshold
        assert high.probability > low.probability
        assert high.take is True
        assert low.take is False

    def test_the_wrong_card_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "meta.json"
        path.write_text('{"kind": "meta_label"}')
        with pytest.raises(ValueError, match="not a ledger-gate card"):
            load_card(path)

    def test_an_unfamiliar_strategy_is_allowed_through(self, tmp_path: Path) -> None:
        gate = fit_gate(planted_edge(300))
        decision = gate.decide({}, strategy="S9_brand_new", symbol="BTC/USDT", moment=START)

        assert decision.take is True
        assert "never in the ledger" in decision.reason


class _Signal:
    def __init__(self, strategy: str = "S1_ema_trend", symbol: str = "BTC/USDT") -> None:
        self.strategy = strategy
        self.symbol = symbol
        self.stop = Decimal("98")
        self.target = Decimal("104")
        self.ts = START
        self.confidence = Decimal("0.5")


class _Context:
    def __init__(self, mark: str = "100", atr: float = 0.8) -> None:

        self.mark = Decimal(mark)
        self.atr = atr


class TestEntryFilter:
    def _filter(self) -> LedgerEntryFilter:
        return LedgerEntryFilter(gate=fit_gate(planted_edge(300)))

    def test_it_vetoes_the_setup_the_ledger_taught_it_to_refuse(self) -> None:
        entry_filter = self._filter()
        allowed, reason = entry_filter(_Signal(), _Context(atr=0.2))  # 20 bps: the loser

        assert allowed is False
        assert "ledger P(win)" in reason
        assert entry_filter.vetoes == 1

    def test_it_allows_the_setup_that_paid(self) -> None:
        entry_filter = self._filter()
        allowed, _ = entry_filter(_Signal(), _Context(atr=0.9))  # 90 bps: the winner

        assert allowed is True
        assert entry_filter.vetoes == 0

    def test_a_broken_context_fails_open_and_says_why(self) -> None:
        entry_filter = self._filter()
        allowed, reason = entry_filter(_Signal(), _Context(mark="0"))

        assert allowed is True
        assert "no mark price" in reason

    def test_a_gate_with_no_vocabulary_blocks_nothing(self) -> None:
        from tradebot.ai.meta import LogisticGate

        empty = LedgerGate(model=LogisticGate(), vocabulary=Vocabulary(), threshold=0.5)
        allowed, reason = LedgerEntryFilter(gate=empty)(_Signal(), _Context())

        assert allowed is True
        assert "not ready" in reason


class TestStaleness:
    def _card(self, tmp_path: Path, created: str) -> Path:
        report = LedgerReport(trades=planted_edge(300))
        verdict = judge(report, walk_forward(report.trades, windows=3))
        path = save_card(tmp_path / "card.json", fit_gate(report.trades), verdict, report=report)
        import json

        payload = json.loads(path.read_text())
        payload["created_at"] = created
        path.write_text(json.dumps(payload))
        return path

    def test_a_fresh_card_is_used(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        fresh = datetime.now(UTC).isoformat()
        gate = load_card(self._card(tmp_path, fresh), max_age_days=14)

        assert gate.ready
        assert gate.stale_reason == ""

    def test_an_old_card_stops_vetoing_and_says_so(self, tmp_path: Path) -> None:
        gate = load_card(self._card(tmp_path, "2020-01-01T00:00:00+00:00"), max_age_days=14)

        assert not gate.ready
        assert "days old" in gate.stale_reason
        decision = gate.decide({}, strategy="S1_ema_trend", symbol="BTC/USDT", moment=START)
        assert decision.take is True
        assert "re-run" in decision.reason

    def test_a_card_without_a_date_cannot_be_age_checked(self, tmp_path: Path) -> None:
        path = self._card(tmp_path, "2020-01-01T00:00:00+00:00")
        import json

        payload = json.loads(path.read_text())
        payload.pop("created_at")
        path.write_text(json.dumps(payload))

        gate = load_card(path, max_age_days=14)

        assert not gate.ready
        assert "no creation date" in gate.stale_reason

    def test_no_limit_means_the_card_is_trusted(self, tmp_path: Path) -> None:
        gate = load_card(self._card(tmp_path, "2020-01-01T00:00:00+00:00"))

        assert gate.ready

    def test_a_stale_card_lets_entries_through(self, tmp_path: Path) -> None:
        gate = load_card(self._card(tmp_path, "2020-01-01T00:00:00+00:00"), max_age_days=14)
        entry_filter = LedgerEntryFilter(gate=gate)

        allowed, reason = entry_filter(_Signal(), _Context(atr=0.2))  # would be vetoed if fresh

        assert allowed is True
        assert "days old" in reason
        assert entry_filter.vetoes == 0
