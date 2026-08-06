from typing import Any

from app.features.spread import top_levels


def market_depth(book: dict[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for n in (5, 10, 20):
        bids = top_levels(book, "bids", n)
        asks = top_levels(book, "asks", n)
        result[f"bid_depth_{n}"] = sum(x["size"] for x in bids)
        result[f"ask_depth_{n}"] = sum(x["size"] for x in asks)
        result[f"depth_{n}"] = result[f"bid_depth_{n}"] + result[f"ask_depth_{n}"]
    return result


def primary_depth(book: dict[str, Any], n: int = 10) -> float | None:
    depths = market_depth(book)
    return depths.get(f"depth_{n}")
