"""Load chart series from fetch_live parquet dirs for live backfill."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.config import settings


def fetch_live_root() -> Path:
    return Path(settings.fetch_live_data_dir)


def fetch_live_market_dir(market_id: str | None) -> Path | None:
    if not market_id:
        return None
    root = fetch_live_root()
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


def scrub_leading_outcome_extremes(
    points: list[dict[str, Any]],
    *,
    lo: float = 0.02,
    hi: float = 0.98,
) -> list[dict[str, Any]]:
    """
    At market open the CLOB often prints 1¢/99¢ placeholders before a real book
    forms. Null those leading extremes so Up/Down charts don't spike 0↔100.
    """
    if not points:
        return points
    out = [dict(p) for p in points]

    def _extreme(p: dict[str, Any]) -> bool:
        u, d = p.get("up"), p.get("down")
        if u is None and d is None:
            return True
        if u is not None and (u <= lo or u >= hi):
            return True
        if d is not None and (d <= lo or d >= hi):
            return True
        return False

    i = 0
    while i < len(out) and _extreme(out[i]):
        out[i]["up"] = None
        out[i]["down"] = None
        i += 1
    return out


def break_outcome_jumps(
    points: list[dict[str, Any]], *, max_jump: float = 0.45
) -> list[dict[str, Any]]:
    """
    Break Up/Down continuity across huge one-step jumps (prior-window bleed).
    Only the jumped sample is nulled; prev advances so a real move to 99¢/1¢
    near resolution does not cascade-null the rest of the window.
    """
    if len(points) < 2:
        return points
    out = [dict(p) for p in points]
    prev_u = out[0].get("up")
    prev_d = out[0].get("down")
    for i in range(1, len(out)):
        u, d = out[i].get("up"), out[i].get("down")
        if (
            prev_u is not None
            and u is not None
            and abs(float(u) - float(prev_u)) > max_jump
        ):
            prev_u = u
            out[i]["up"] = None
            u = None
        elif u is not None:
            prev_u = u
        if (
            prev_d is not None
            and d is not None
            and abs(float(d) - float(prev_d)) > max_jump
        ):
            prev_d = d
            out[i]["down"] = None
            d = None
        elif d is not None:
            prev_d = d
    return out

