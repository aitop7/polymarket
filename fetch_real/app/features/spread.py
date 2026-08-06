from typing import Any


def compute_spread(best_bid: float | None, best_ask: float | None) -> float | None:
    if best_bid is None or best_ask is None:
        return None
    return best_ask - best_bid


def mid_price(best_bid: float | None, best_ask: float | None) -> float | None:
    if best_bid is None or best_ask is None:
        return None
    return (best_bid + best_ask) / 2.0


def top_levels(book: dict[str, Any], side: str, n: int = 20) -> list[dict[str, float]]:
    levels = book.get(side) or book.get(side.rstrip("s")) or []
    out: list[dict[str, float]] = []
    for level in levels[:n]:
        if isinstance(level, dict):
            price = float(level.get("price") or level.get("p") or 0)
            size = float(level.get("size") or level.get("s") or 0)
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, size = float(level[0]), float(level[1])
        else:
            continue
        out.append({"price": price, "size": size})
    return out
