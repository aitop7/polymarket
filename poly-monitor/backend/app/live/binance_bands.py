"""Binance USD-distance quantity bands (matches fetch_live binance_price_orderbook)."""

from __future__ import annotations

from typing import Any

# Widths 0.1…51.2 → edges 0, 0.1, 0.3, …, 102.3 (same as fetch_live/app/schemas.py).
BINANCE_BAND_WIDTHS_USD: tuple[float, ...] = (
    0.1,
    0.2,
    0.4,
    0.8,
    1.6,
    3.2,
    6.4,
    12.8,
    25.6,
    51.2,
)


def _band_edges() -> list[float]:
    edges = [0.0]
    for w in BINANCE_BAND_WIDTHS_USD:
        edges.append(round(edges[-1] + float(w), 10))
    return edges


BINANCE_BAND_EDGES_USD: tuple[float, ...] = tuple(_band_edges())
BINANCE_BAND_CLOSED_SUFFIXES: tuple[str, ...] = tuple(
    f"{int(round(BINANCE_BAND_EDGES_USD[i] * 10))}_"
    f"{int(round(BINANCE_BAND_EDGES_USD[i + 1] * 10))}"
    for i in range(len(BINANCE_BAND_WIDTHS_USD))
)
BINANCE_BAND_PLUS_SUFFIX = f"{int(round(BINANCE_BAND_EDGES_USD[-1] * 10))}_"
BINANCE_BAND_SUFFIXES: tuple[str, ...] = (
    *BINANCE_BAND_CLOSED_SUFFIXES,
    BINANCE_BAND_PLUS_SUFFIX,
)

BINANCE_BAND_META: tuple[dict[str, Any], ...] = tuple(
    {
        "suffix": BINANCE_BAND_CLOSED_SUFFIXES[i],
        "lo_usd": BINANCE_BAND_EDGES_USD[i],
        "hi_usd": BINANCE_BAND_EDGES_USD[i + 1],
        "mid_usd": (BINANCE_BAND_EDGES_USD[i] + BINANCE_BAND_EDGES_USD[i + 1]) / 2.0,
    }
    for i in range(len(BINANCE_BAND_WIDTHS_USD))
) + (
    {
        "suffix": BINANCE_BAND_PLUS_SUFFIX,
        "lo_usd": BINANCE_BAND_EDGES_USD[-1],
        "hi_usd": None,
        "mid_usd": BINANCE_BAND_EDGES_USD[-1] + 25.6,
    },
)


def _bucket_suffix(dist: float) -> str | None:
    if dist < 0:
        return None
    edges = BINANCE_BAND_EDGES_USD
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        if lo <= dist < hi:
            return BINANCE_BAND_CLOSED_SUFFIXES[i]
    if dist >= edges[-1]:
        return BINANCE_BAND_PLUS_SUFFIX
    return None


def _fmt_usd_range(lo: float, hi: float | None, *, open_high: bool = False) -> str:
    lo_v = round(lo, 2)
    if open_high or hi is None:
            return f"${lo_v:,.2f}+"
    hi_v = round(hi, 2)
    if abs(lo_v - hi_v) < 1e-9:
        return f"${lo_v:,.2f}"
    return f"${lo_v:,.2f}-${hi_v:,.2f}"


def bucket_levels(
    levels: list[tuple[float, float]],
    *,
    mid: float,
    side: str,
) -> dict[str, float]:
    totals = {s: 0.0 for s in BINANCE_BAND_SUFFIXES}
    for px, qty in levels:
        if qty <= 0:
            continue
        dist = (px - mid) if side == "ask" else (mid - px)
        suffix = _bucket_suffix(dist)
        if suffix is None:
            continue
        totals[suffix] += qty
    return totals


def banded_book_from_levels(
    *,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    mid: float,
    best_bid: float | None,
    best_ask: float | None,
    timestamp_ms: int,
    symbol: str = "BTCUSDT",
) -> dict[str, Any]:
    """Build UI payload: absolute USD ranges + BTC qty per distance band."""
    ask_qty = bucket_levels(asks, mid=mid, side="ask")
    bid_qty = bucket_levels(bids, mid=mid, side="bid")

    ask_rows: list[dict[str, Any]] = []
    bid_rows: list[dict[str, Any]] = []
    for meta in BINANCE_BAND_META:
        suffix = str(meta["suffix"])
        lo = float(meta["lo_usd"])
        hi = meta["hi_usd"]
        mid_off = float(meta["mid_usd"])
        a_qty = float(ask_qty.get(suffix) or 0.0)
        b_qty = float(bid_qty.get(suffix) or 0.0)

        ask_lo = mid + lo
        ask_hi = None if hi is None else mid + float(hi)
        ask_approx = mid + mid_off
        ask_rows.append(
            {
                "range": _fmt_usd_range(ask_lo, ask_hi, open_high=hi is None),
                "suffix": suffix,
                "lo_usd": lo,
                "hi_usd": hi,
                "price_lo": round(ask_lo, 4),
                "price_hi": None if ask_hi is None else round(ask_hi, 4),
                "qty": a_qty,
                "approx_price": round(ask_approx, 4),
                "notional": round(a_qty * ask_approx, 2),
            }
        )

        if hi is None:
            bid_hi = mid - lo
            bid_approx = mid - mid_off
            bid_rows.append(
                {
                    "range": f"≤${bid_hi:,.2f}",
                    "suffix": suffix,
                    "lo_usd": lo,
                    "hi_usd": None,
                    "price_lo": None,
                    "price_hi": round(bid_hi, 4),
                    "qty": b_qty,
                    "approx_price": round(bid_approx, 4),
                    "notional": round(b_qty * bid_approx, 2),
                }
            )
        else:
            bid_lo = mid - float(hi)
            bid_hi = mid - lo
            bid_approx = mid - mid_off
            bid_rows.append(
                {
                    "range": _fmt_usd_range(bid_lo, bid_hi),
                    "suffix": suffix,
                    "lo_usd": lo,
                    "hi_usd": float(hi),
                    "price_lo": round(bid_lo, 4),
                    "price_hi": round(bid_hi, 4),
                    "qty": b_qty,
                    "approx_price": round(bid_approx, 4),
                    "notional": round(b_qty * bid_approx, 2),
                }
            )

    # Asks: farthest from mid at top (reverse); bids: nearest first.
    asks_view = list(reversed(ask_rows))
    bids_view = bid_rows

    ask_total = sum(r["qty"] for r in ask_rows)
    bid_total = sum(r["qty"] for r in bid_rows)
    spread = None
    if best_bid is not None and best_ask is not None:
        spread = max(0.0, float(best_ask) - float(best_bid))

    return {
        "symbol": symbol,
        "timestamp": int(timestamp_ms),
        "mode": "usd_distance_bands",
        "note": (
            "BTC quantity in USD-distance bands from mid "
            "(same schema as binance_price_orderbook.parquet)."
        ),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "asks": asks_view,
        "bids": bids_view,
        "ask_qty": ask_total,
        "bid_qty": bid_total,
    }
