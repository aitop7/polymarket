"""Aggregate Binance + Polymarket trade tape into chart volume buckets."""

from __future__ import annotations

import bisect
from pathlib import Path
from typing import Any

import pandas as pd

# Chart series keys
BN_BUY = "bn_buy"
BN_SELL = "bn_sell"
UP_BUY = "up_buy_vol"
UP_SELL = "up_sell_vol"
DOWN_BUY = "down_buy_vol"
DOWN_SELL = "down_sell_vol"

VOLUME_KEYS = (BN_BUY, BN_SELL, UP_BUY, UP_SELL, DOWN_BUY, DOWN_SELL)
# One volume bar per 5 seconds on charts.
VOLUME_BUCKET_MS = 5_000


def _empty_bucket() -> dict[str, float]:
    return {k: 0.0 for k in VOLUME_KEYS}


def load_volume_buckets(
    market_dir: Path | None, *, bucket_ms: int = VOLUME_BUCKET_MS
) -> dict[int, dict[str, float]]:
    """
    Fixed-width buckets of executed trade volume (default 5s).
    Binance: quantity (BTC); taker buy = not buyer_is_maker, taker sell = buyer_is_maker.
    Polymarket (fetch_live): token False=UP True=DOWN; side False=BUY True=SELL; shares.
    Keys are bucket start timestamps.
    """
    if market_dir is None or not market_dir.is_dir():
        return {}
    buckets: dict[int, dict[str, float]] = {}
    bucket_ms = max(1, int(bucket_ms))

    def bucket(ts: int) -> dict[str, float]:
        key = (int(ts) // bucket_ms) * bucket_ms
        b = buckets.get(key)
        if b is None:
            b = _empty_bucket()
            buckets[key] = b
        return b

    bn_path = market_dir / "binance_trades.parquet"
    if not bn_path.is_file():
        bn_path = market_dir / "btc_trades.parquet"
    if bn_path.is_file():
        try:
            df = pd.read_parquet(bn_path, columns=["timestamp", "quantity", "buyer_is_maker"])
            if not df.empty and "timestamp" in df.columns and "quantity" in df.columns:
                for row in df.itertuples(index=False):
                    try:
                        ts = int(getattr(row, "timestamp"))
                        qty = float(getattr(row, "quantity") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    if qty <= 0:
                        continue
                    b = bucket(ts)
                    maker = bool(getattr(row, "buyer_is_maker", False))
                    if maker:
                        b[BN_SELL] += qty
                    else:
                        b[BN_BUY] += qty
        except Exception:
            pass

    tr_path = market_dir / "trades.parquet"
    if tr_path.is_file():
        try:
            df = pd.read_parquet(
                tr_path, columns=["timestamp", "token", "side", "shares"]
            )
            if not df.empty and "timestamp" in df.columns and "shares" in df.columns:
                for row in df.itertuples(index=False):
                    try:
                        ts = int(getattr(row, "timestamp"))
                        shares = float(getattr(row, "shares") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    if shares <= 0:
                        continue
                    token = bool(getattr(row, "token", False))
                    side = bool(getattr(row, "side", False))
                    b = bucket(ts)
                    if not token and not side:
                        b[UP_BUY] += shares
                    elif not token and side:
                        b[UP_SELL] += shares
                    elif token and not side:
                        b[DOWN_BUY] += shares
                    else:
                        b[DOWN_SELL] += shares
        except Exception:
            pass

    return buckets


def attach_volumes_to_series(
    series: list[dict[str, Any]],
    buckets: dict[int, dict[str, float]],
    *,
    bucket_ms: int = VOLUME_BUCKET_MS,
) -> list[dict[str, Any]]:
    """
    Place each 5s bucket's volume on the last series point inside that bucket.
    Other points get 0 so the volume chart shows one bar per 5 seconds.
    Bar timestamp = bucket end (start + bucket_ms) when no series point lands in-bucket.
    """
    if not series:
        return series
    bucket_ms = max(1, int(bucket_ms))
    for p in series:
        for k in VOLUME_KEYS:
            p[k] = 0.0
    if not buckets:
        return series

    # Map bucket_start -> index of last series point with t in (start, start+bucket_ms]
    last_idx: dict[int, int] = {}
    for i, p in enumerate(series):
        t = int(p["t"])
        start = (t // bucket_ms) * bucket_ms
        # point at exact bucket start belongs to previous bucket's end edge
        if t == start and start > 0:
            start = start - bucket_ms
        last_idx[start] = i

    for start, vols in buckets.items():
        i = last_idx.get(int(start))
        if i is None:
            # No price sample in this 5s window — synthesize at bucket end if inside series span
            end_t = int(start) + bucket_ms
            t0 = int(series[0]["t"])
            t1 = int(series[-1]["t"])
            if end_t < t0 or int(start) > t1:
                continue
            # Attach to nearest series point at or after bucket end, else last
            times = [int(p["t"]) for p in series]
            j = bisect.bisect_left(times, end_t)
            if j >= len(series):
                j = len(series) - 1
            i = j
        for k in VOLUME_KEYS:
            series[i][k] = float(vols.get(k) or 0.0) + float(series[i].get(k) or 0.0)
    return series


def volumes_for_market_id(market_id: str | None) -> dict[int, dict[str, float]]:
    if not market_id:
        return {}
    from app.core.live_dataset import find_live_market_dir
    from app.live.fetch_live_series import fetch_live_market_dir

    d = find_live_market_dir(str(market_id)) or fetch_live_market_dir(str(market_id))
    return load_volume_buckets(d, bucket_ms=VOLUME_BUCKET_MS)
