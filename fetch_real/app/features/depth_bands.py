"""Flat, storage-optimized orderbook snapshot rows (no JSON / nested types).

Liquidity buckets are distance-from-traded-price in cents:

  ask: +0~1, +1~3, +3~7, +7~15, +15~30, +30+
  bid: -0~1, -1~3, -3~7, -7~15, -15~30, -30+
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from app.features.spread import mid_price, top_levels
from app.utils.time import datetime_to_ms


# (lo_cents_inclusive, hi_cents_exclusive) distance from ref; last uses hi=None = +inf
DISTANCE_BUCKETS: list[tuple[int, int | None]] = [
    (0, 1),
    (1, 3),
    (3, 7),
    (7, 15),
    (15, 30),
    (30, None),
]

BUCKET_SUFFIXES = ("0_1", "1_3", "3_7", "7_15", "15_30", "30_plus")


def orderbook_column_names() -> list[str]:
    cols = [
        "timestamp",
        "up_price",
        "down_price",
        "up_bid_price",
        "up_bid_shares",
        "up_ask_price",
        "up_ask_shares",
        "down_bid_price",
        "down_bid_shares",
        "down_ask_price",
        "down_ask_shares",
    ]
    for side in ("up", "down"):
        for kind in ("ask", "bid"):
            for suffix in BUCKET_SUFFIXES:
                cols.append(f"{side}_{kind}_{suffix}")
    return cols


ORDERBOOK_COLUMNS = orderbook_column_names()


def timestamp_to_ms(ts: Any) -> int:
    if isinstance(ts, (int, float)):
        v = int(ts)
        # treat seconds as < 1e12
        return v if v > 10_000_000_000 else v * 1000
    if isinstance(ts, datetime):
        return datetime_to_ms(ts)
    return datetime_to_ms(pd_to_datetime(ts))


def pd_to_datetime(ts: Any) -> datetime:
    import pandas as pd

    dt = pd.to_datetime(ts, utc=True).to_pydatetime()
    return dt


def _price_cents(price: float) -> float:
    return float(price) * 100.0


def _shares_u32(size: float | None) -> int:
    if size is None:
        return 0
    v = int(round(float(size)))
    return max(0, min(v, 2**32 - 1))


def _f32(price: float | None) -> float | None:
    if price is None:
        return None
    return float(price)


def levels_from_prints(
    prints: list[tuple[float, float]],
    *,
    reverse: bool,
) -> list[dict[str, float]]:
    agg: dict[float, float] = defaultdict(float)
    for price, size in prints:
        agg[round(float(price), 3)] += float(size)
    prices = sorted(agg.keys(), reverse=reverse)
    return [{"price": p, "size": float(agg[p])} for p in prices[:50]]


def ref_price_from_book(book: dict[str, Any] | None, fallback: float | None = None) -> float | None:
    if not book:
        return fallback
    bids = top_levels(book, "bids", 1)
    asks = top_levels(book, "asks", 1)
    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    mid = mid_price(best_bid, best_ask)
    if mid is not None:
        return float(mid)
    if best_bid is not None:
        return float(best_bid)
    if best_ask is not None:
        return float(best_ask)
    return fallback


def _bucket_shares(
    levels: list[dict[str, float]],
    *,
    ref_price: float,
    side: str,
) -> dict[str, int]:
    """side='ask' => prices >= ref; side='bid' => prices <= ref. Distance in cents."""
    ref_c = _price_cents(ref_price)
    totals = {suffix: 0.0 for suffix in BUCKET_SUFFIXES}
    for level in levels:
        px = float(level["price"])
        sz = float(level["size"])
        c = _price_cents(px)
        if side == "ask":
            dist = c - ref_c
            if dist < 0:
                continue
        else:
            dist = ref_c - c
            if dist < 0:
                continue
        for (lo, hi), suffix in zip(DISTANCE_BUCKETS, BUCKET_SUFFIXES):
            if hi is None:
                if dist >= lo:
                    totals[suffix] += sz
                    break
            elif lo <= dist < hi:
                totals[suffix] += sz
                break
    return {k: _shares_u32(v) for k, v in totals.items()}


def side_flat_fields(
    prefix: str,
    *,
    bids: list[dict[str, float]],
    asks: list[dict[str, float]],
    traded_price: float | None,
) -> dict[str, Any]:
    ref = traded_price
    if ref is None:
        ref = ref_price_from_book({"bids": bids, "asks": asks})
    if ref is None:
        ref = 0.5

    best_bid = bids[0] if bids else None
    best_ask = asks[0] if asks else None
    out: dict[str, Any] = {
        f"{prefix}_price": _f32(traded_price),
        f"{prefix}_bid_price": _f32(best_bid["price"] if best_bid else None),
        f"{prefix}_bid_shares": _shares_u32(best_bid["size"] if best_bid else 0),
        f"{prefix}_ask_price": _f32(best_ask["price"] if best_ask else None),
        f"{prefix}_ask_shares": _shares_u32(best_ask["size"] if best_ask else 0),
    }
    ask_buckets = _bucket_shares(asks, ref_price=ref, side="ask")
    bid_buckets = _bucket_shares(bids, ref_price=ref, side="bid")
    for suffix in BUCKET_SUFFIXES:
        out[f"{prefix}_ask_{suffix}"] = ask_buckets[suffix]
        out[f"{prefix}_bid_{suffix}"] = bid_buckets[suffix]
    return out


def build_orderbook_row(
    *,
    timestamp: Any,
    up_bids: list[dict[str, float]] | None = None,
    up_asks: list[dict[str, float]] | None = None,
    down_bids: list[dict[str, float]] | None = None,
    down_asks: list[dict[str, float]] | None = None,
    up_price: float | None = None,
    down_price: float | None = None,
) -> dict[str, Any]:
    """One flat snapshot row matching ORDERBOOK_COLUMNS."""
    row: dict[str, Any] = {"timestamp": timestamp_to_ms(timestamp)}
    row.update(
        side_flat_fields(
            "up",
            bids=up_bids or [],
            asks=up_asks or [],
            traded_price=up_price,
        )
    )
    row.update(
        side_flat_fields(
            "down",
            bids=down_bids or [],
            asks=down_asks or [],
            traded_price=down_price,
        )
    )
    # ensure every column present
    for col in ORDERBOOK_COLUMNS:
        if col not in row or row[col] is None:
            if col == "timestamp":
                continue
            if col.endswith("_shares") or any(col.endswith(f"_{s}") for s in BUCKET_SUFFIXES):
                row[col] = 0
            else:
                row[col] = None
        elif col.endswith("_shares") or any(col.endswith(f"_{s}") for s in BUCKET_SUFFIXES):
            row[col] = int(row[col] or 0)
    return {c: row.get(c) for c in ORDERBOOK_COLUMNS}
