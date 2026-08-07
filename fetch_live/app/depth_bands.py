"""Distance-from-traded-price orderbook buckets (storage columns only)."""

from __future__ import annotations

from typing import Any

from app.schemas import BUCKET_SUFFIXES, ORDERBOOK_COLUMNS

DISTANCE_BUCKETS: list[tuple[int, int | None]] = [
    (0, 1),
    (1, 3),
    (3, 7),
    (7, 15),
    (15, 30),
    (30, None),
]


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


def _best(levels: list[dict[str, float]], *, reverse: bool) -> dict[str, float] | None:
    if not levels:
        return None
    return sorted(levels, key=lambda x: x["price"], reverse=reverse)[0]


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return bid if bid is not None else ask


def _bucket_shares(
    levels: list[dict[str, float]],
    *,
    ref_price: float,
    side: str,
) -> dict[str, int]:
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
    best_bid = _best(bids, reverse=True)
    best_ask = _best(asks, reverse=False)
    ref = traded_price
    if ref is None:
        ref = _mid(
            best_bid["price"] if best_bid else None,
            best_ask["price"] if best_ask else None,
        )
    if ref is None:
        ref = 0.5

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
    timestamp_ms: int,
    up_bids: list[dict[str, float]] | None = None,
    up_asks: list[dict[str, float]] | None = None,
    down_bids: list[dict[str, float]] | None = None,
    down_asks: list[dict[str, float]] | None = None,
    up_price: float | None = None,
    down_price: float | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"timestamp": int(timestamp_ms)}
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
