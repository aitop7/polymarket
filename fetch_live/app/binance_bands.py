"""Binance order book USD-distance quantity bands from mid."""

from __future__ import annotations

from typing import Any

from app.schemas import (
    BINANCE_BAND_COLUMNS,
    BINANCE_BAND_CLOSED_SUFFIXES,
    BINANCE_BAND_EDGES_USD,
    BINANCE_BAND_PLUS_SUFFIX,
    BINANCE_BAND_SUFFIXES,
)


def _bucket_suffix(dist: float) -> str | None:
    """Map distance-from-mid (USD) to band suffix; overflow → 1023_."""
    if dist < 0:
        return None
    edges = BINANCE_BAND_EDGES_USD
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        if lo <= dist < hi:
            return BINANCE_BAND_CLOSED_SUFFIXES[i]
    # dist >= last edge
    if dist >= edges[-1]:
        return BINANCE_BAND_PLUS_SUFFIX
    return None


def build_binance_band_fields(
    bids: list[dict[str, float]] | dict[float, float],
    asks: list[dict[str, float]] | dict[float, float],
    mid: float,
) -> dict[str, float]:
    """Sum BTC size into ask_* / bid_* USD-distance bands from mid."""
    ask_tot = {s: 0.0 for s in BINANCE_BAND_SUFFIXES}
    bid_tot = {s: 0.0 for s in BINANCE_BAND_SUFFIXES}

    def _iter_levels(
        levels: list[dict[str, float]] | dict[float, float],
    ) -> list[tuple[float, float]]:
        if isinstance(levels, dict):
            return [(float(p), float(q)) for p, q in levels.items() if float(q) > 0]
        out: list[tuple[float, float]] = []
        for lv in levels:
            px = float(lv["price"])
            sz = float(lv["size"])
            if sz > 0:
                out.append((px, sz))
        return out

    for px, sz in _iter_levels(asks):
        suffix = _bucket_suffix(px - mid)
        if suffix is None:
            continue
        ask_tot[suffix] += sz

    for px, sz in _iter_levels(bids):
        suffix = _bucket_suffix(mid - px)
        if suffix is None:
            continue
        bid_tot[suffix] += sz

    out: dict[str, Any] = {}
    for suffix in BINANCE_BAND_SUFFIXES:
        out[f"ask_{suffix}"] = float(ask_tot[suffix])
        out[f"bid_{suffix}"] = float(bid_tot[suffix])
    return {c: float(out.get(c) or 0.0) for c in BINANCE_BAND_COLUMNS}
