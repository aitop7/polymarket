"""Load chart series from fetch_live parquet dirs for live backfill."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd

_FETCH_LIVE_DATA = Path(__file__).resolve().parents[4] / "fetch_live" / "data"


def fetch_live_market_dir(market_id: str | None) -> Path | None:
    if not market_id:
        return None
    root = _FETCH_LIVE_DATA
    if not root.is_dir():
        return None
    mid = str(market_id)
    for day in sorted(root.iterdir(), reverse=True)[:6]:
        if not day.is_dir():
            continue
        d = day / mid
        if d.is_dir():
            return d
    return None


def _finite(v: Any) -> float | None:
    if v is None:
        return None
    try:
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return x


def load_fetch_live_series(market_id: str | None) -> list[dict[str, Any]]:
    """
    Join chainlink / binance / orderbooks parquets into chart points:
    { t, twap, chainlink, btc, up, down }.
    """
    d = fetch_live_market_dir(market_id)
    if d is None:
        return []

    frames: list[pd.DataFrame] = []
    cl_path = d / "chainlink_price.parquet"
    if cl_path.is_file():
        try:
            cl = pd.read_parquet(cl_path, columns=["timestamp", "Chainlink_BTC", "twap"])
            cl = cl.rename(columns={"Chainlink_BTC": "chainlink"})
            frames.append(cl)
        except Exception:
            pass

    bn_path = d / "binance_price_orderbook.parquet"
    if bn_path.is_file():
        try:
            bn = pd.read_parquet(bn_path, columns=["timestamp", "Binance_BTC"])
            bn = bn.rename(columns={"Binance_BTC": "btc"})
            frames.append(bn)
        except Exception:
            pass

    ob_path = d / "orderbooks.parquet"
    if ob_path.is_file():
        try:
            ob = pd.read_parquet(ob_path, columns=["timestamp", "up_price", "down_price"])
            ob = ob.rename(columns={"up_price": "up", "down_price": "down"})
            frames.append(ob)
        except Exception:
            pass

    if not frames:
        return []

    df = frames[0]
    for extra in frames[1:]:
        df = df.merge(extra, on="timestamp", how="outer")
    df = df.sort_values("timestamp", kind="mergesort").reset_index(drop=True)

    out: list[dict[str, Any]] = []
    for row in df.itertuples(index=False):
        t = _finite(getattr(row, "timestamp", None))
        if t is None:
            continue
        point = {
            "t": int(t),
            "twap": _finite(getattr(row, "twap", None)) if hasattr(row, "twap") else None,
            "chainlink": _finite(getattr(row, "chainlink", None))
            if hasattr(row, "chainlink")
            else None,
            "btc": _finite(getattr(row, "btc", None)) if hasattr(row, "btc") else None,
            "up": _finite(getattr(row, "up", None)) if hasattr(row, "up") else None,
            "down": _finite(getattr(row, "down", None)) if hasattr(row, "down") else None,
        }
        # Skip empty rows (no series values at all).
        if all(point[k] is None for k in ("twap", "chainlink", "btc", "up", "down")):
            continue
        out.append(point)
    return out


def merge_series(
    base: list[dict[str, Any]], overlay: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge by timestamp; overlay non-null fields win."""
    by_t: dict[int, dict[str, Any]] = {}
    for p in base:
        t = int(p["t"])
        by_t[t] = dict(p)
    for p in overlay:
        t = int(p["t"])
        cur = by_t.get(t)
        if cur is None:
            by_t[t] = dict(p)
            continue
        for k, v in p.items():
            if k == "t":
                continue
            if v is not None:
                cur[k] = v
    return [by_t[t] for t in sorted(by_t)]
