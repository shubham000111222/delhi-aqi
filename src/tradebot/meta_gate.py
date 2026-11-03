"""Wiring a saved meta-label gate into a running engine.

`ai/meta.py` decides; `engine.py` enforces the veto; this module joins them, and it lives
outside `ai/` because building a feature frame from live market data means touching the
market hub - which the AI layer is not allowed to import.

The features are computed from the engine's own bar window and its latest book snapshot,
cached per symbol and per bar close. Recomputing two dozen features for every signal would
turn a 15-minute strategy into a CPU-bound one, and the features only change when a bar
closes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from .ai.features import FeatureFrame, FeatureStore
from .ai.meta import MetaEntryFilter, MetaGate, load_meta
from .core.logging import get_logger

log = get_logger(__name__)

#: Fewer bars than this and the rolling windows are mostly NaN.
MIN_BARS = 100


def frame_builder(
    engine: Any, *, store: FeatureStore | None = None, min_bars: int = MIN_BARS
) -> Callable[[str], FeatureFrame | None]:
    """A cached `symbol -> FeatureFrame` builder over the engine's market hub."""
    features = store or FeatureStore()
    cache: dict[str, tuple[datetime, FeatureFrame | None]] = {}

    def build(symbol: str) -> FeatureFrame | None:
        market = engine.market
        candles = market.candles(symbol, market.base_timeframe).last(market.series_maxlen)
        if len(candles) < min_bars:
            return None
        last_ts = candles[-1].ts
        cached = cache.get(symbol)
        if cached is not None and cached[0] == last_ts:
            return cached[1]
        book = market.book(symbol)
        books = {symbol: {book.ts: book}} if book is not None else None
        try:
            frames = features.compute_many({symbol: candles}, books=books)
        except Exception as exc:  # noqa: BLE001 - a feature fault must not stop trading
            log.warning('{"event":"meta_features_failed","symbol":"%s","error":"%s"}', symbol, type(exc).__name__)
            cache[symbol] = (last_ts, None)
            return None
        frame = frames.get(symbol)
        cache[symbol] = (last_ts, frame)
        return frame

    return build


def attach(engine: Any, path: str, *, store: FeatureStore | None = None) -> MetaEntryFilter:
    """Load a meta model card and install it as the engine's veto-only entry filter."""
    gate: MetaGate = load_meta(path)
    entry_filter = MetaEntryFilter(gate=gate, frame_for=frame_builder(engine, store=store))
    engine.entry_filter = entry_filter
    log.info(
        '{"event":"meta_gate_attached","name":"%s","threshold":%.3f,"features":%d}',
        gate.name,
        gate.threshold,
        len(gate.features),
    )
    return entry_filter


__all__ = ["attach", "frame_builder"]
