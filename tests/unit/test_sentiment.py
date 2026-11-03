"""Tests for the sentiment overlay: dedupe, decay, and the "reduce only" rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tradebot.ai.sentiment import (
    Headline,
    LexiconScorer,
    SentimentOverlay,
    SentimentStore,
    overlay_from_provider,
    parse_simple_rss,
    score_headlines,
)

NOW = datetime(2024, 1, 10, 12, 0, tzinfo=UTC)


def headline(text: str, *, hours_ago: float = 0.0, symbol: str = "BTC/USDT") -> Headline:
    return Headline(text=text, ts=NOW - timedelta(hours=hours_ago), symbol=symbol)


class TestLexicon:
    def test_good_news_is_positive(self):
        assert LexiconScorer().score("Bitcoin surges to a record high") > 0

    def test_bad_news_is_negative(self):
        assert LexiconScorer().score("Exchange hacked, funds lost in massive exploit") < 0

    def test_a_negation_flips_the_score(self):
        plain = LexiconScorer().score("The upgrade was approved")
        negated = LexiconScorer().score("The upgrade was not approved")
        assert negated < plain

    def test_neutral_text_scores_zero(self):
        assert LexiconScorer().score("Bitcoin trades sideways on Tuesday") == 0.0

    def test_the_score_is_bounded(self):
        assert -1.0 <= LexiconScorer().score("surge rally soar gain jump bull inflow") <= 1.0

    def test_empty_text_is_zero(self):
        assert LexiconScorer().score("") == 0.0


class TestStore:
    def test_duplicate_stories_are_counted_once(self):
        store = SentimentStore()
        assert store.add(headline("Bitcoin surges")) is True
        assert store.add(headline("BITCOIN surges!")) is False, "same story, different punctuation"
        assert store.score("BTC/USDT", NOW)["headlines"] == 1

    def test_stale_headlines_decay_to_nothing(self):
        store = SentimentStore(half_life=timedelta(hours=1))
        store.add(headline("Exchange hacked", hours_ago=40))
        assert store.score("BTC/USDT", NOW)["headlines"] == 0

    def test_recent_news_dominates_old_news(self):
        store = SentimentStore(half_life=timedelta(hours=6))
        store.add(headline("Bitcoin plunges on hack news", hours_ago=20))
        store.add(headline("Bitcoin surges on approval", hours_ago=0.1))
        assert store.score("BTC/USDT", NOW)["score"] > 0

    def test_market_wide_news_applies_to_every_symbol(self):
        store = SentimentStore()
        store.add(Headline("Markets crash as ban announced", NOW, symbol=""))
        assert store.score("ETH/USDT", NOW)["headlines"] == 1

    def test_other_symbols_news_is_ignored(self):
        store = SentimentStore()
        store.add(headline("Solana hacked", symbol="SOL/USDT"))
        assert store.score("BTC/USDT", NOW)["headlines"] == 0

    def test_a_future_headline_is_ignored(self):
        store = SentimentStore()
        store.add(Headline("Bitcoin surges", NOW + timedelta(hours=1)))
        assert store.score("BTC/USDT", NOW)["headlines"] == 0

    def test_volume_spike_is_detected(self):
        store = SentimentStore()
        for hour in (5, 6, 7):
            store.add(headline(f"old news {hour}", hours_ago=hour))
        for i in range(6):
            store.add(headline(f"breaking story {i}", hours_ago=0.05))
        assert store.volume_zscore("BTC/USDT", NOW) > 1.0

    def test_a_quiet_feed_has_no_volume_signal(self):
        store = SentimentStore()
        store.add(headline("one story", hours_ago=1))
        assert store.volume_zscore("BTC/USDT", NOW) == 0.0


class TestOverlay:
    def _overlay(self, records: list[Headline]) -> SentimentOverlay:
        store = SentimentStore()
        store.add_many(records)
        return SentimentOverlay(store=store)

    def test_no_news_means_no_change(self):
        result = self._overlay([]).modifier("BTC/USDT", NOW)
        assert result["multiplier"] == 1.0
        assert "no relevant headlines" in result["reason"]

    def test_bad_news_reduces_size(self):
        result = self._overlay([headline("Exchange hacked and funds stolen in exploit")]).modifier(
            "BTC/USDT", NOW
        )
        assert result["multiplier"] < 1.0
        assert "negative sentiment" in result["reason"]

    def test_good_news_never_increases_size(self):
        """The guardrail: sentiment may reduce risk, never add it."""
        result = self._overlay([headline("Bitcoin surges to record high on approval")]).modifier(
            "BTC/USDT", NOW
        )
        assert result["multiplier"] == 1.0

    def test_the_multiplier_never_goes_below_the_floor(self):
        overlay = self._overlay([headline("crash plunge hack exploit bankrupt fraud", hours_ago=0.01)])
        assert overlay.modifier("BTC/USDT", NOW)["multiplier"] >= overlay.floor

    def test_a_headline_spike_cuts_size_even_when_neutral(self):
        records = [headline(f"bitcoin update {i}", hours_ago=0.02) for i in range(8)]
        records += [headline("old", hours_ago=5)]
        result = self._overlay(records).modifier("BTC/USDT", NOW)
        assert result["multiplier"] <= 1.0
        assert "spike" in result["reason"] or result["multiplier"] == 1.0


class TestRss:
    RSS = """<?xml version="1.0"?><rss><channel>
      <item><title>Bitcoin surges past resistance</title>
        <pubDate>Wed, 10 Jan 2024 10:00:00 +0000</pubDate></item>
      <item><title>No date here</title></item>
    </channel></rss>"""

    def test_items_without_a_date_are_dropped(self):
        headlines = parse_simple_rss(self.RSS)
        assert len(headlines) == 1
        assert headlines[0].text == "Bitcoin surges past resistance"
        assert headlines[0].ts.tzinfo is not None

    def test_markup_inside_titles_is_stripped(self):
        xml = self.RSS.replace("Bitcoin surges past resistance", "<b>Bitcoin</b> surges")
        assert parse_simple_rss(xml)[0].text == "Bitcoin surges"

    def test_garbage_does_not_crash(self):
        assert parse_simple_rss("not xml at all") == []

    def test_batch_scoring_works_without_a_store(self):
        rows = score_headlines([headline("Bitcoin surges"), headline("Exchange hacked")])
        assert rows[0]["score"] > 0 > rows[1]["score"]

    def test_provider_builds_a_working_overlay(self):
        overlay = overlay_from_provider(lambda: [headline("Exchange hacked in exploit")])
        result = overlay.modifier("BTC/USDT", NOW)
        assert result["multiplier"] <= 1.0
        assert "sentiment" in result["reason"]


class TestEngineHook:
    """The overlay may reduce an approved size. It can never increase one."""

    def _engine(self):
        import asyncio

        from tradebot.config import load_config
        from tradebot.core.clock import SimClock
        from tradebot.core.models import OrderType, Side, Signal
        from tradebot.core.money import D
        from tradebot.core.store import Store
        from tradebot.engine import Engine

        cfg = load_config()

        async def _build():
            return await Engine.build(cfg, clock=SimClock(NOW), store=Store(":memory:"))

        # asyncio.run rather than get_event_loop(): other tests close the loop, and a
        # cached closed loop is exactly the failure this caused the first time
        engine = asyncio.run(_build())
        rules = engine.adapter.get_market_rules("BTC/USDT")
        signal = Signal(
            id="sig1",
            strategy="S1_ema_trend",
            symbol="BTC/USDT",
            timeframe="1h",
            side=Side.BUY,
            entry_type=OrderType.LIMIT,
            ts=NOW,
            stop=D("1900"),
            target=D("2200"),
            limit_price=D("2000"),
        )
        return engine, rules, signal, D

    def test_no_overlay_changes_nothing(self, ):
        engine, rules, signal, D = self._engine()
        assert engine._apply_sentiment(signal, D("1"), rules) == D("1")

    def test_bad_news_shrinks_the_order(self, ):
        engine, rules, signal, D = self._engine()
        store = SentimentStore()
        store.add(Headline("Exchange hacked, funds stolen in exploit", NOW, symbol="BTC/USDT"))
        engine.sentiment = SentimentOverlay(store=store)
        assert engine._apply_sentiment(signal, D("1"), rules) < D("1")

    def test_good_news_cannot_grow_the_order(self, ):
        engine, rules, signal, D = self._engine()
        store = SentimentStore()
        store.add(Headline("Bitcoin surges on record approval", NOW, symbol="BTC/USDT"))
        engine.sentiment = SentimentOverlay(store=store)
        assert engine._apply_sentiment(signal, D("1"), rules) == D("1")

    def test_a_broken_overlay_does_not_stop_trading(self, ):
        engine, rules, signal, D = self._engine()

        class Broken:
            def modifier(self, symbol, now):
                raise RuntimeError("news feed on fire")

        engine.sentiment = Broken()
        assert engine._apply_sentiment(signal, D("1"), rules) == D("1")
