"""Tests for the venue seams: calendar, instruments, costs, sessions, square-off."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tradebot.core.money import D
from tradebot.venues import (
    AlwaysOpenCalendar,
    CryptoCostModel,
    Instrument,
    NSECalendar,
    NSECostModel,
    Product,
    SquareOffScheduler,
    breakeven_move,
    compare_models,
    round_trip,
)
from tradebot.venues.session import InteractiveSession, SessionError, StaticSession, Token


def ist(hour: int, minute: int = 0, day: int = 1, month: int = 1, year: int = 2024) -> datetime:
    """A UTC instant that reads as the given IST wall-clock time."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC) - timedelta(hours=5, minutes=30)


class TestNSECalendar:
    def test_session_is_0915_to_1530_ist(self):
        calendar = NSECalendar()
        assert calendar.is_open(ist(9, 15))
        assert calendar.is_open(ist(15, 29))
        assert not calendar.is_open(ist(9, 14))
        assert not calendar.is_open(ist(15, 30))
        assert not calendar.is_open(ist(15, 45))

    def test_weekends_and_holidays_are_closed(self):
        holiday = date(2024, 1, 26)  # Republic Day
        calendar = NSECalendar(holidays=[holiday])
        assert not calendar.is_open(ist(11, 0, day=6))  # Saturday
        assert not calendar.is_open(datetime(2024, 1, 26, 6, 0, tzinfo=UTC))
        assert calendar.is_trading_day(date(2024, 1, 29))

    def test_next_open_skips_the_closed_days(self):
        # Saturday 6 Jan -> Monday 8 Jan, unless Monday is a holiday
        calendar = NSECalendar()
        local = calendar.next_open(ist(12, 0, day=6)).astimezone(calendar.tz)
        assert local.date() == date(2024, 1, 8)
        assert (local.hour, local.minute) == (9, 15)

        # with Monday 8 Jan declared a holiday, the answer moves to Tuesday 9 Jan
        holiday_calendar = NSECalendar(holidays=[date(2024, 1, 8)])
        local = holiday_calendar.next_open(ist(12, 0, day=6)).astimezone(holiday_calendar.tz)
        assert local.date() == date(2024, 1, 9)

    def test_seconds_to_close(self):
        calendar = NSECalendar()
        remaining = calendar.seconds_to_close(ist(15, 0))
        assert remaining == pytest.approx(30 * 60)


class TestAlwaysOpen:
    def test_crypto_is_always_open_and_has_no_close(self):
        calendar = AlwaysOpenCalendar()
        assert calendar.is_open(datetime(2024, 1, 6, 3, 0, tzinfo=UTC))  # a Saturday, 08:30 IST
        assert calendar.next_close(datetime(2024, 1, 6, 3, 0, tzinfo=UTC)) is None


class TestInstrument:
    def test_price_snaps_to_the_nearest_tick(self):
        instrument = Instrument("X", tick_size=D("0.05"))
        assert instrument.round_price(D("100.02")) == D("100.00")  # 0.02 away, vs 0.03
        assert instrument.round_price(D("100.04")) == D("100.05")  # 0.01 away, vs 0.04
        assert instrument.round_price(D("100.00")) == D("100.00")

    def test_quantity_rounds_down_never_up(self):
        instrument = Instrument("X", lot_size=D("5"))
        assert instrument.round_qty(D("12")) == D("10")

    def test_band_rejects_a_move_beyond_the_circuit(self):
        instrument = Instrument("X", tick_size=D("0.05"), price_band_pct=D("0.20"))
        low, high = instrument.band(D("1000"))
        assert (low, high) == (D("800.00"), D("1200.00"))
        assert instrument.validate(qty=D("1"), price=D("1300"), reference_price=D("1000"))

    def test_validate_collects_every_reason(self):
        instrument = Instrument("X", tick_size=D("0.05"), lot_size=D("5"), min_qty=D("5"))
        problems = instrument.validate(qty=D("3"), price=D("10.01"), reference_price=D("10"))
        assert any("lot" in p for p in problems)
        assert any("minimum" in p for p in problems)
        assert any("tick" in p for p in problems)

    def test_crypto_instrument_has_no_band(self):
        from tradebot.venues.instruments import crypto_instrument

        instrument = crypto_instrument(
            "BTC/USDT", tick_size=D("0.01"), lot_step=D("0.00001"), min_qty=D("0.00001")
        )
        assert instrument.price_band_pct is None
        assert instrument.product is Product.NORMAL


class TestCosts:
    def test_crypto_is_a_percentage_per_side(self):
        model = CryptoCostModel(maker_fee="0.001", taker_fee="0.001")
        breakdown = model.charges(notional=D("1000"), side="buy")
        assert breakdown.total == D("1.0000")
        assert round_trip(model, entry_notional=D("1000"), exit_notional=D("1000")).total == D("2.0000")

    def test_nse_intraday_charges_every_component(self):
        breakdown = NSECostModel().charges(notional=D("100000"), side="sell", product="intraday")
        assert breakdown.brokerage == D("20")  # capped at Rs 20
        assert breakdown.stt > 0  # only on the sell side
        assert breakdown.gst > 0
        assert breakdown.dp == 0  # no DP charge intraday

    def test_nse_intraday_buy_has_no_stt_but_has_stamp(self):
        breakdown = NSECostModel().charges(notional=D("100000"), side="buy", product="intraday")
        assert breakdown.stt == 0
        assert breakdown.stamp > 0

    def test_delivery_sell_has_stt_on_both_sides_and_dp_charge(self):
        model = NSECostModel()
        buy = model.charges(notional=D("100000"), side="buy", product="delivery")
        sell = model.charges(notional=D("100000"), side="sell", product="delivery")
        assert buy.stt > 0 and sell.stt > 0
        assert sell.dp == D("15.34")

    def test_breakeven_is_the_cost_of_a_round_trip(self):
        move = breakeven_move(CryptoCostModel(), notional=D("1000"))
        assert move == D("0.002")  # 0.1% in, 0.1% out

    def test_comparison_table_is_complete(self):
        rows = compare_models(D("100000"), [CryptoCostModel(), NSECostModel()])
        assert {row["model"] for row in rows} == {"crypto", "nse"}
        assert all("total" in row for row in rows)


class TestSessions:
    async def test_static_session_never_expires(self):
        session = StaticSession()
        token = await session.ensure(datetime(2024, 1, 1, tzinfo=UTC))
        assert token.valid(datetime(2024, 6, 1, tzinfo=UTC))

    async def test_interactive_login_requires_a_human(self):
        session = InteractiveSession(login_fn=None)
        with pytest.raises(SessionError):
            await session.login()

    async def test_interactive_session_renews_and_records_attempts(self):
        calls: list[int] = []

        def login() -> str:
            calls.append(1)
            return "token-abc"

        session = InteractiveSession(login_fn=login)
        first = await session.ensure(datetime(2024, 1, 1, 4, 0, tzinfo=UTC))
        assert first.value == "token-abc"
        # the same day, well inside the session: no second login
        await session.ensure(datetime(2024, 1, 1, 6, 0, tzinfo=UTC))
        assert len(calls) == 1
        assert session.attempts[-1].ok is True
        assert "token-abc" not in repr(first), "the token must never leak through repr"

    def test_token_expiry_is_the_end_of_the_trading_day(self):
        session = InteractiveSession()
        expiry = session.expiry_for(datetime(2024, 1, 1, 4, 0, tzinfo=UTC))
        assert expiry == datetime(2024, 1, 1, 10, 0, tzinfo=UTC)

    def test_token_validity_with_margin(self):
        now = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
        token = Token(value="x", issued_at=now, expires_at=now + timedelta(minutes=10))
        assert token.valid(now)
        assert not token.valid(now, margin=timedelta(minutes=15))


class TestSquareOff:
    def test_intraday_must_be_flat_before_the_close(self):
        scheduler = SquareOffScheduler(NSECalendar(), lead_minutes=20)
        assert not scheduler.should_flatten(ist(14, 0))
        assert scheduler.should_flatten(ist(15, 15))
        plan = scheduler.plan(ist(15, 15))
        assert plan is not None and plan.due_now

    def test_no_square_off_when_the_market_is_shut(self):
        scheduler = SquareOffScheduler(NSECalendar(), lead_minutes=20)
        assert not scheduler.should_flatten(ist(16, 30))
        assert scheduler.plan(ist(16, 30)) is not None  # a plan exists, it is just not due

    def test_crypto_has_no_square_off(self):
        scheduler = SquareOffScheduler(AlwaysOpenCalendar())
        assert scheduler.plan(datetime(2024, 1, 1, tzinfo=UTC)) is None
        assert scheduler.should_flatten(datetime(2024, 1, 1, tzinfo=UTC)) is False

    def test_missed_square_off_is_visible(self):
        scheduler = SquareOffScheduler(NSECalendar())
        assert scheduler.missed(ist(15, 45))
        assert not scheduler.missed(ist(12, 0))

    def test_next_deadline_rolls_to_the_following_session(self):
        scheduler = SquareOffScheduler(NSECalendar(), lead_minutes=20)
        deadline = scheduler.next_session_deadline(ist(16, 0))
        assert deadline is not None
        local = deadline.astimezone(scheduler.calendar.tz)
        assert (local.hour, local.minute) == (15, 10)
        assert local.date() > date(2024, 1, 1)


class TestEngineVenueHook:
    async def test_closed_venue_blocks_entries(self, cfg):
        from tradebot.core.clock import SimClock
        from tradebot.core.store import Store
        from tradebot.data.feed import SyntheticFeed
        from tradebot.engine import Engine

        clock = SimClock(ist(16, 0))  # after the NSE close
        store = Store(":memory:")
        engine = await Engine.build(cfg, clock=clock, store=store, feed=SyntheticFeed(cfg, clock))
        engine.calendar = NSECalendar()
        note = engine.market_session_note()
        assert note is not None and "closed" in note
        # crypto default: always open
        engine.calendar = AlwaysOpenCalendar()
        assert engine.market_session_note() is None
        engine.close()
