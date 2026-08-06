from __future__ import annotations

from datetime import datetime
from typing import Any

from app.features.depth import market_depth, primary_depth
from app.features.depth_bands import (
    ORDERBOOK_COLUMNS,
    build_orderbook_row,
    levels_from_prints,
    ref_price_from_book,
    timestamp_to_ms,
)
from app.features.imbalance import order_imbalance
from app.features.meta_schema import META_KEYS, build_meta_document, encode_winner
from app.features.momentum import MomentumTracker
from app.features.spread import compute_spread, mid_price, top_levels
from app.features.trade_schema import TRADE_COLUMNS, build_trade_row
from app.features.volatility import VolatilityTracker
from app.features.whale import whale_score
from app.utils.time import utcnow


class FeatureEngine:
    def __init__(self) -> None:
        self._momentum: dict[str, MomentumTracker] = {}
        self._volatility: dict[str, VolatilityTracker] = {}
        self._last_trade_size: dict[str, float] = {}

    def note_trade(self, market_id: str, size: float, price: float) -> None:
        self._last_trade_size[market_id] = whale_score(size, price)

    def compute(
        self,
        *,
        market_id: str,
        book: dict[str, Any],
        settlement_time: datetime | None = None,
        timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        ts = timestamp or utcnow()
        bids = top_levels(book, "bids", 1)
        asks = top_levels(book, "asks", 1)
        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None
        spread = compute_spread(best_bid, best_ask)
        mid = mid_price(best_bid, best_ask)
        imbalance = order_imbalance(book)
        depth = primary_depth(book)
        depths = market_depth(book)

        mom = None
        vol = None
        extras: dict[str, Any] = {"depths": depths}
        if mid is not None:
            m_tracker = self._momentum.setdefault(market_id, MomentumTracker())
            v_tracker = self._volatility.setdefault(market_id, VolatilityTracker())
            moms = m_tracker.update(ts, mid)
            vols = v_tracker.update(ts, mid)
            mom = moms.get("mom_5s")
            vol = vols.get("realized_vol")
            extras["momentum"] = moms
            extras["volatility"] = vols

        time_remaining = None
        if settlement_time is not None:
            time_remaining = max(0.0, (settlement_time - ts).total_seconds())

        return {
            "timestamp": ts,
            "market_id": market_id,
            "spread": spread,
            "imbalance": imbalance,
            "momentum": mom,
            "volatility": vol,
            "depth": depth,
            "whale_score": self._last_trade_size.get(market_id, 0.0),
            "time_remaining": time_remaining,
            "extras": extras,
            "best_bid": best_bid,
            "best_ask": best_ask,
        }


__all__ = [
    "FeatureEngine",
    "compute_spread",
    "mid_price",
    "order_imbalance",
    "market_depth",
    "MomentumTracker",
    "VolatilityTracker",
    "whale_score",
    "ORDERBOOK_COLUMNS",
    "build_orderbook_row",
    "levels_from_prints",
    "ref_price_from_book",
    "timestamp_to_ms",
    "TRADE_COLUMNS",
    "build_trade_row",
    "META_KEYS",
    "build_meta_document",
    "encode_winner",
]
