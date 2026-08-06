from typing import Any

from app.features.spread import top_levels


def order_imbalance(book: dict[str, Any], depth: int = 20) -> float | None:
    bids = top_levels(book, "bids", depth)
    asks = top_levels(book, "asks", depth)
    bid_vol = sum(x["size"] for x in bids)
    ask_vol = sum(x["size"] for x in asks)
    total = bid_vol + ask_vol
    if total <= 0:
        return None
    return bid_vol / total
