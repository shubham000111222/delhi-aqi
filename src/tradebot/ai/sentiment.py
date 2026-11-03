"""Sentiment overlay (§5, §19.4): a *size* modifier, never a trade generator.

The rule the plan insists on: sentiment may reduce risk, never create it. So everything
here returns a multiplier in ``[0.5, 1.0]``. There is no code path in which a bullish
headline makes a position bigger, and that is deliberate - headlines are the least
reliable input in the system, and a model that can enlarge a position based on them is a
model that can blow up the account on a fake tweet.

Components:

* `Headline` - what arrived, when, from where, de-duplicated by content hash;
* `LexiconScorer` - a small, transparent finance lexicon. It is a *baseline*, and the
  docstring says so: it cannot read "not a hack, funds are safe", so it must not be
  trusted the way a trained classifier would be;
* `FinBertScorer` - the real thing (§19.4), imported lazily. It is not exercised in this
  repository because the model weights cannot be downloaded in the build sandbox, and
  pretending otherwise would be exactly the sort of thing this project avoids;
* `SentimentStore` - recency-weighted, time-decayed aggregation per symbol with a
  headline-volume z-score, because "many headlines at once" is itself a risk signal;
* `SentimentOverlay` - turns the store into a size multiplier plus a short explanation
  that ends up in the trade reason and the daily report.

Stale data is treated as *no* data: a headline older than the half-life several times over
contributes nothing, and a feed that has gone quiet for hours does not silently keep its
last score.
"""

from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

EPS = 1e-9

#: Words that move a score. Deliberately small and boring: a long list of hand-tuned
#: words is a fitted model in disguise, and this one has no validation behind it.
POSITIVE = {
    "surge": 1.0, "rally": 1.0, "soar": 1.0, "gain": 0.6, "gains": 0.6, "jump": 0.7,
    "record": 0.5, "approve": 0.8, "approved": 0.8, "adoption": 0.6, "upgrade": 0.7,
    "bullish": 1.0, "inflow": 0.6, "inflows": 0.6, "partnership": 0.5, "launch": 0.4,
    "buyback": 0.6, "breakout": 0.6, "recover": 0.5, "recovery": 0.5, "profit": 0.5,
}
NEGATIVE = {
    "crash": 1.0, "plunge": 1.0, "dump": 0.8, "hack": 1.0, "hacked": 1.0, "exploit": 0.9,
    "lawsuit": 0.8, "sue": 0.7, "ban": 1.0, "banned": 1.0, "outflow": 0.6, "outflows": 0.6,
    "bearish": 1.0, "selloff": 0.9, "sell-off": 0.9, "liquidation": 0.7, "delist": 1.0,
    "fraud": 1.0, "insolvent": 1.0, "bankrupt": 1.0, "probe": 0.6, "fine": 0.5,
    "downgrade": 0.7, "halt": 0.6, "stall": 0.4, "loss": 0.5, "losses": 0.5,
}
NEGATORS = {"not", "no", "never", "denies", "denied", "without", "isn't", "wasn't"}


@dataclass(frozen=True)
class Headline:
    text: str
    ts: datetime
    source: str = "unknown"
    symbol: str = ""  # empty means "market-wide"
    url: str = ""

    @property
    def key(self) -> str:
        """Content hash: the same story from two outlets is one event."""
        normalised = re.sub(r"[^a-z0-9 ]", "", self.text.lower()).strip()
        return hashlib.sha1(normalised.encode()).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "ts": self.ts.isoformat(),
            "source": self.source,
            "symbol": self.symbol,
            "key": self.key,
        }


class SentimentScorer(Protocol):
    name: str

    def score(self, text: str) -> float:
        """A score in [-1, 1]."""
        ...


@dataclass
class LexiconScorer:
    """Transparent word-count baseline. Weak, and says so."""

    name: str = "lexicon-v1"

    def score(self, text: str) -> float:
        words = re.findall(r"[a-z][a-z'\-]*", text.lower())
        total = 0.0
        for index, word in enumerate(words):
            # plural and participle forms: "surges" and "plunges" are the same news as
            # "surge" and "plunge", and a lexicon that misses them is just a worse lexicon
            base = word[:-1] if len(word) > 3 and word.endswith("s") else word
            value = (
                POSITIVE.get(word, POSITIVE.get(base, 0.0))
                - NEGATIVE.get(word, NEGATIVE.get(base, 0.0))
            )
            if value == 0.0:
                continue
            window = words[max(0, index - 3) : index]
            if any(negation in window for negation in NEGATORS):
                value = -value * 0.8  # a negation flips it, but less confidently
            total += value
        if not words:
            return 0.0
        # squash: one dramatic headline should not saturate the overlay
        return max(-1.0, min(1.0, total / max(2.0, math.sqrt(len(words)))))


@dataclass
class FinBertScorer:
    """The §19.4 model, loaded lazily and never assumed to be present.

    Not exercised in this repository: the weights need a download the build sandbox does
    not allow. The pipeline (which is the part worth testing) is the same either way.
    """

    model_name: str = "ProsusAI/finbert"
    name: str = "finbert"
    _pipeline: Any = None

    def _load(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        try:
            from transformers import pipeline
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "FinBERT needs `pip install transformers torch` and the model weights; "
                "use LexiconScorer until it is available"
            ) from exc
        self._pipeline = pipeline("sentiment-analysis", model=self.model_name)
        return self._pipeline

    def score(self, text: str) -> float:  # pragma: no cover - needs weights
        result = self._load()(text[:512])[0]
        label = str(result.get("label", "")).lower()
        confidence = float(result.get("score", 0.0))
        if label.startswith("pos"):
            return confidence
        if label.startswith("neg"):
            return -confidence
        return 0.0


@dataclass
class SentimentStore:
    """Deduplicated, time-decayed headlines per symbol."""

    half_life: timedelta = timedelta(hours=6)
    max_items: int = 2000
    scorer: SentimentScorer = field(default_factory=LexiconScorer)
    _items: deque[Headline] = field(default_factory=lambda: deque(maxlen=2000))
    _seen: dict[str, datetime] = field(default_factory=dict)
    _cache: dict[str, float] = field(default_factory=dict)

    def add(self, headline: Headline) -> bool:
        """``False`` when the story was already seen (deduplicated by content)."""
        if headline.key in self._seen:
            return False
        self._seen[headline.key] = headline.ts
        self._items.append(headline)
        if headline.text not in self._cache:
            self._cache[headline.text] = self.scorer.score(headline.text)
        return True

    def add_many(self, headlines: Iterable[Headline]) -> int:
        return sum(1 for headline in headlines if self.add(headline))

    def _weight(self, headline: Headline, now: datetime) -> float:
        age = (now - headline.ts).total_seconds()
        if age < 0:
            return 0.0  # a headline from the future is a clock bug, not a signal
        half_life = max(0.0, self.half_life.total_seconds())
        if half_life <= 0:
            return 1.0
        return 0.5 ** (age / half_life)

    def score(self, symbol: str, now: datetime) -> dict[str, Any]:
        relevant = [
            headline
            for headline in self._items
            if headline.symbol in ("", symbol) and headline.ts <= now
        ]
        weighted, weights = 0.0, 0.0
        recent: list[dict[str, Any]] = []
        for headline in relevant:
            weight = self._weight(headline, now)
            if weight < 0.02:  # effectively stale
                continue
            value = self._cache.get(headline.text, 0.0)
            weighted += value * weight
            weights += weight
            recent.append({"text": headline.text, "score": round(value, 3), "weight": round(weight, 3)})
        aggregate = weighted / weights if weights else 0.0
        return {
            "symbol": symbol,
            "score": round(aggregate, 4),
            "headlines": len(recent),
            "weight": round(weights, 3),
            "examples": sorted(recent, key=lambda item: -abs(item["score"]))[:3],
        }

    def volume_zscore(self, symbol: str, now: datetime, *, window: timedelta = timedelta(days=3)) -> float:
        """How unusual the current headline flow is: a spike means uncertainty."""
        counts: dict[int, int] = {}
        for headline in self._items:
            if headline.symbol in ("", symbol):
                bucket = int((now - headline.ts).total_seconds() // 3600)
                if 0 <= bucket < window.total_seconds() / 3600:
                    counts[bucket] = counts.get(bucket, 0) + 1
        if len(counts) < 3:
            return 0.0
        values = list(counts.values())
        mean = statistics.fmean(values)
        stdev = statistics.pstdev(values)
        current = counts.get(0, 0)
        return (current - mean) / stdev if stdev > EPS else 0.0


@dataclass
class SentimentOverlay:
    """Converts sentiment into a size multiplier in [0.5, 1.0] - never above 1."""

    store: SentimentStore
    floor: float = 0.5
    #: sentiment below this starts to cut size; above it, sentiment is ignored
    negative_threshold: float = -0.15
    volume_z_cut: float = 2.0

    def modifier(self, symbol: str, now: datetime) -> dict[str, Any]:
        summary = self.store.score(symbol, now)
        score = float(summary["score"])
        volume_z = self.store.volume_zscore(symbol, now)
        reasons: list[str] = []
        multiplier = 1.0

        if summary["headlines"] == 0:
            return {
                "multiplier": 1.0,
                "score": 0.0,
                "volume_z": round(volume_z, 2),
                "reason": "no relevant headlines",
            }

        if score < self.negative_threshold:
            # linear between the floor and "ignore", so nothing is a cliff
            span = abs(self.negative_threshold) or 1.0
            severity = min(1.0, (self.negative_threshold - score) / span)
            multiplier = max(self.floor, 1.0 - severity * (1.0 - self.floor))
            reasons.append(f"negative sentiment {score:+.2f}")
        if volume_z > self.volume_z_cut:
            multiplier = max(self.floor, multiplier * 0.8)
            reasons.append(f"headline spike (z {volume_z:+.1f})")

        return {
            "multiplier": round(multiplier, 3),
            "score": round(score, 4),
            "volume_z": round(volume_z, 2),
            "reason": "; ".join(reasons) if reasons else "sentiment neutral",
            "examples": summary["examples"],
        }


def parse_simple_rss(xml: str, *, source: str = "rss", symbol: str = "") -> list[Headline]:
    """A deliberately small RSS reader: ``<item><title>`` and ``<pubDate>``.

    Written by hand to avoid an XML parser that expands entities from the network, and
    because a news feed is untrusted input: only text nodes are read, and a missing date
    makes the item *unusable* rather than "now" (which would make old news look fresh).
    """
    from email.utils import parsedate_to_datetime

    headlines: list[Headline] = []
    for item in re.findall(r"<item>(.*?)</item>", xml, flags=re.DOTALL | re.IGNORECASE):
        title_match = re.search(r"<title>(.*?)</title>", item, flags=re.DOTALL | re.IGNORECASE)
        date_match = re.search(r"<pubDate>(.*?)</pubDate>", item, flags=re.DOTALL | re.IGNORECASE)
        if not title_match or not date_match:
            continue
        title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
        try:
            published = parsedate_to_datetime(date_match.group(1).strip())
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            continue  # naive timestamps are not trustworthy across sources
        headlines.append(Headline(text=title, ts=published, source=source, symbol=symbol))
    return headlines


def score_headlines(
    headlines: Iterable[Headline], *, scorer: SentimentScorer | None = None
) -> list[dict[str, Any]]:
    """Score a batch without a store: useful for backfills and for eyeballing a lexicon."""
    scorer = scorer or LexiconScorer()
    return [
        {"text": headline.text, "score": round(scorer.score(headline.text), 3), "key": headline.key}
        for headline in headlines
    ]


def overlay_from_provider(
    provider: Callable[[], Iterable[Headline]],
    *,
    scorer: SentimentScorer | None = None,
    half_life: timedelta = timedelta(hours=6),
) -> SentimentOverlay:
    """Build an overlay from a headline provider (an RSS poll, an API client, a test)."""
    store = SentimentStore(half_life=half_life, scorer=scorer or LexiconScorer())
    store.add_many(provider())
    return SentimentOverlay(store=store)


def explain(overlay: SentimentOverlay, symbols: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """A report fragment: what sentiment did to each symbol's size today."""
    return {
        symbol: overlay.modifier(symbol, now)
        for symbol in symbols
    }
