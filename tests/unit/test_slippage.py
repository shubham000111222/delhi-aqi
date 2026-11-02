"""Tests for slippage recording and calibration."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.models import BookLevel, Fill, OrderBook, Side
from tradebot.core.money import D
from tradebot.execution.slippage import (
    SlippageModel,
    SlippageRecorder,
    bucket_for,
    calibrate_from_fills,
    depth_at_best,
    implied_bps,
    size_for_budget,
)

TS = datetime(2024, 1, 1, tzinfo=UTC)


def book(symbol: str = "BTC/USDT", bid_depth: str = "10", ask_depth: str = "10") -> OrderBook:
    return OrderBook(
        symbol=symbol,
        ts=TS,
        bids=(BookLevel(price=D("100.0"), qty=D(bid_depth)),),
        asks=(BookLevel(price=D("100.1"), qty=D(ask_depth)),),
    )


def fill(price: str, expected: str | None, side: Side = Side.BUY, qty: str = "1",
         symbol: str = "BTC/USDT") -> Fill:
    return Fill(
        order_id="o1",
        symbol=symbol,
        side=side,
        price=D(price),
        qty=D(qty),
        fee=D("0"),
        ts=TS,
        expected_price=D(expected) if expected is not None else None,
    )


class TestImpliedSlippage:
    def test_a_buy_above_expectation_is_adverse(self):
        assert implied_bps(fill("101", "100")) == pytest.approx(100.0)

    def test_a_buy_below_expectation_is_favourable(self):
        assert implied_bps(fill("99", "100")) < 0

    def test_signs_flip_for_a_sell(self):
        # selling below the expected price is the adverse case
        assert implied_bps(fill("99", "100", side=Side.SELL)) == pytest.approx(100.0)

    def test_no_expected_price_falls_back(self):
        assert implied_bps(fill("101", None)) == 0.0

    def test_zero_prices_are_ignored(self):
        assert implied_bps(fill("0", "0")) == 0.0


class TestBuckets:
    def test_sizes_map_to_the_largest_edge_they_do_not_exceed(self):
        assert bucket_for(0.05) == 0.1
        assert bucket_for(0.1) == 0.1
        assert bucket_for(0.3) == 0.25
        assert bucket_for(9.0) == 5.0

    def test_depth_reads_the_side_we_trade_against(self):
        assert depth_at_best(book(ask_depth="7"), Side.BUY) == 7.0
        assert depth_at_best(book(bid_depth="3"), Side.SELL) == 3.0
        assert depth_at_best(None, Side.BUY) == 0.0


class TestRecorder:
    def test_fills_without_an_expected_price_are_counted_not_guessed(self):
        recorder = SlippageRecorder()
        recorder.observe(fill("101", None))
        assert recorder.records == []
        assert recorder.skipped == 1

    def test_records_carry_the_depth_fraction(self):
        recorder = SlippageRecorder()
        recorder.observe(fill("100.1", "100.0", qty="2"), book=book(ask_depth="10"))
        record = recorder.records[0]
        assert record.depth_fraction == pytest.approx(0.2)
        assert record.bps == pytest.approx(10.0, rel=1e-6)

    def test_summary_reports_the_distribution(self):
        recorder = SlippageRecorder()
        for price in ("100.0", "100.1", "100.2"):
            recorder.observe(fill(price, "100.0"))
        summary = recorder.summary()
        assert summary["fills"] == 3
        assert summary["worst_bps"] == pytest.approx(20.0, rel=1e-6)
        assert summary["median_bps"] == pytest.approx(10.0, rel=1e-6)

    def test_an_empty_recorder_says_so(self):
        assert SlippageRecorder().summary()["fills"] == 0


class TestModel:
    def _recorder(self) -> SlippageRecorder:
        recorder = SlippageRecorder()
        recorder.observe(fill("100.1", "100.0", qty="1"), book=book(ask_depth="10"))  # 10 bps, 0.1x
        recorder.observe(fill("100.3", "100.0", qty="3"), book=book(ask_depth="10"))  # 30 bps, 0.3x
        recorder.observe(fill("100.5", "100.0", qty="8"), book=book(ask_depth="10"))  # 50 bps, 0.8x
        return recorder

    def test_fit_builds_a_bucket_per_size_class(self):
        model = self._recorder().model()
        assert set(model.buckets["BTC/USDT"]) == {0.1, 0.25, 0.5}

    def test_predictions_never_flatter(self):
        model = self._recorder().model()
        small = model.predict_bps("BTC/USDT", D("0.5"), book(ask_depth="10"), Side.BUY)
        large = model.predict_bps("BTC/USDT", D("9"), book(ask_depth="10"), Side.BUY)
        assert large >= small

    def test_an_unknown_symbol_gets_the_conservative_fallback(self):
        model = self._recorder().model()
        assert model.predict_bps("DOGE/USDT", D("1"), None, Side.BUY) == model.fallback_bps

    def test_a_missing_book_assumes_the_worst_bucket(self):
        model = self._recorder().model()
        predicted = model.predict_bps("BTC/USDT", D("1"), None, Side.BUY)
        assert predicted == max(bucket.p95_bps for bucket in model.buckets["BTC/USDT"].values())

    def test_calibration_flags_an_optimistic_model(self):
        recorder = self._recorder()
        model = SlippageModel.fit([])
        model.fallback_bps = 0.0
        result = recorder.calibration(model)
        assert result["samples"] == 3
        assert "optimistic" in result["verdict"]

    def test_calibration_is_not_alarmed_by_a_conservative_model(self):
        recorder = self._recorder()
        pessimistic = recorder.model()
        for buckets in pessimistic.buckets.values():
            for bucket in buckets.values():
                bucket.p95_bps += 100.0
        assert "not systematically optimistic" in recorder.calibration(pessimistic)["verdict"]

    def test_table_rows_are_reportable(self):
        rows = self._recorder().model().table()
        assert rows and {"symbol", "size_vs_depth_up_to", "p95_bps"} <= set(rows[0])


class TestSizing:
    def test_sizing_respects_the_slippage_budget(self):
        recorder = SlippageRecorder()
        recorder.observe(fill("100.1", "100.0", qty="1"), book=book(ask_depth="10"))   # 10 bps
        recorder.observe(fill("100.9", "100.0", qty="9"), book=book(ask_depth="10"))   # 90 bps
        model = recorder.model()
        qty = size_for_budget(model, "BTC/USDT", book(ask_depth="10"), Side.BUY, budget_bps=20)
        assert qty == D("1")  # 0.1 of depth, since the 0.5 bucket costs 90 bps

    def test_no_book_means_no_size(self):
        model = SlippageModel()
        assert size_for_budget(model, "BTC/USDT", book(ask_depth="0"), Side.BUY, budget_bps=20) == D("0")

    def test_an_expensive_venue_gets_no_size_at_all(self):
        model = SlippageModel(fallback_bps=50.0)
        assert size_for_budget(model, "BTC/USDT", book(), Side.BUY, budget_bps=20) == D("0")


class TestEngineIntegration:
    def test_a_market_order_carries_a_reference_price(self):
        """Regression: before the reference price, every market fill measured 0 bps.

        The paper adapter stamps the decision price on the order; a market order that only
        knew its fill price would report zero slippage by construction.
        """
        from tradebot.core.models import Order, OrderStatus, OrderType

        order = Order(
            client_order_id="c1",
            symbol="BTC/USDT",
            side=Side.BUY,
            type=OrderType.MARKET,
            qty=D("1"),
            price=None,
            status=OrderStatus.NEW,
            reference_price=D("100"),
        )
        assert Decimal(str(order.to_dict()["reference_price"])) == D("100")
        assert implied_bps(fill("100.5", str(order.reference_price))) == pytest.approx(50.0)

    def test_calibrate_from_fills_wraps_the_recorder(self):
        model, summary = calibrate_from_fills(
            [fill("100.2", "100.0"), fill("100.4", "100.0")], books={"BTC/USDT": book()}
        )
        assert summary["fills"] == 2
        assert "BTC/USDT" in model.buckets
