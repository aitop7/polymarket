"""Index and load markets from FETCH_LIVE_DATA_DIR (TWAP / live parquet tree)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.config import settings

TWAP_SPLIT = "twap"
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MIN_START_MS = 1_600_000_000_000
_ET = ZoneInfo("America/New_York")


def _ms_to_et_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=_ET).strftime("%Y-%m-%d")


def _ms_to_et_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=_ET).strftime("%H:%M")


def live_data_root() -> Path:
    return Path(settings.fetch_live_data_dir)


def find_live_market_dir(market_id: str) -> Path | None:
    mid = str(market_id).strip()
    if not mid:
        return None
    root = live_data_root()
    if not root.is_dir():
        return None
    days = sorted(
        (d for d in root.iterdir() if d.is_dir() and _DAY_RE.match(d.name)),
        reverse=True,
    )
    for day in days:
        candidate = day / mid
        if candidate.is_dir() and (candidate / "meta.json").is_file():
            return candidate
    return None


def _read_meta(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def iter_live_market_metas() -> list[dict[str, Any]]:
    root = live_data_root()
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for day_dir in sorted(root.iterdir()):
        if not day_dir.is_dir() or not _DAY_RE.match(day_dir.name):
            continue
        for market_dir in day_dir.iterdir():
            if not market_dir.is_dir():
                continue
            meta_path = market_dir / "meta.json"
            if not meta_path.is_file():
                continue
            meta = _read_meta(meta_path)
            if meta is None:
                continue
            try:
                start = int(meta.get("start_time") or 0)
            except (TypeError, ValueError):
                continue
            if start < _MIN_START_MS:
                continue
            try:
                end = int(meta.get("end_time") or 0)
            except (TypeError, ValueError):
                end = 0
            mid = str(meta.get("market_id") or market_dir.name)
            winner = meta.get("winner")
            if winner is True:
                winner_i: int | None = 1
            elif winner is False:
                winner_i = 0
            else:
                winner_i = None
            closed = bool(meta.get("closed")) or winner_i is not None
            open_px = meta.get("btc_open_price")
            try:
                open_f = float(open_px) if open_px is not None else None
            except (TypeError, ValueError):
                open_f = None
            out.append(
                {
                    "market_id": mid,
                    "split": TWAP_SPLIT,
                    "start_time": start,
                    "end_time": end,
                    "date_et": _ms_to_et_date(start),
                    "time_et": _ms_to_et_time(start),
                    "has_features": False,
                    "has_training": True,
                    "rows": None,
                    "winner": winner_i,
                    "closed": closed,
                    "btc_open_price": open_f,
                    "date_utc": day_dir.name,
                    "dir": str(market_dir),
                }
            )
    out.sort(key=lambda r: (int(r["start_time"]), str(r["market_id"])))
    return out


def live_fingerprint() -> dict[str, Any]:
    root = live_data_root()
    if not root.is_dir():
        return {"n": 0, "stamp": 0.0}
    n = 0
    stamp = float(root.stat().st_mtime)
    for day in root.iterdir():
        if not day.is_dir() or not _DAY_RE.match(day.name):
            continue
        stamp = max(stamp, float(day.stat().st_mtime))
        for mid in day.iterdir():
            if mid.is_dir() and (mid / "meta.json").is_file():
                n += 1
    return {"n": n, "stamp": stamp}


def load_live_market_frame(market_id: str) -> pd.DataFrame:
    """Join fetch_live parquets into a monitor/replay-friendly frame."""
    d = find_live_market_dir(market_id)
    if d is None:
        raise FileNotFoundError(f"TWAP market not found: {market_id}")
    meta = _read_meta(d / "meta.json") or {}
    try:
        start = int(meta.get("start_time") or 0)
    except (TypeError, ValueError):
        start = 0
    try:
        end = int(meta.get("end_time") or 0)
    except (TypeError, ValueError):
        end = 0
    open_px = meta.get("btc_open_price")
    try:
        open_f = float(open_px) if open_px is not None else None
    except (TypeError, ValueError):
        open_f = None
    winner = meta.get("winner")
    if winner is True:
        winner_i: int | None = 1
    elif winner is False:
        winner_i = 0
    else:
        winner_i = None

    frames: list[pd.DataFrame] = []
    cl = d / "chainlink_price.parquet"
    if cl.is_file():
        try:
            df = pd.read_parquet(cl)
            keep = [c for c in ("timestamp", "Chainlink_BTC", "twap") if c in df.columns]
            if keep:
                df = df[keep].rename(
                    columns={"Chainlink_BTC": "btc_chainlink", "twap": "btc_twap_30s"}
                )
                frames.append(df)
        except Exception:
            pass

    bn = d / "binance_price_orderbook.parquet"
    if bn.is_file():
        try:
            df = pd.read_parquet(bn)
            if "timestamp" in df.columns and "Binance_BTC" in df.columns:
                frames.append(
                    df[["timestamp", "Binance_BTC"]].rename(
                        columns={"Binance_BTC": "btc_price"}
                    )
                )
        except Exception:
            pass

    ob = d / "orderbooks.parquet"
    if ob.is_file():
        try:
            frames.append(pd.read_parquet(ob))
        except Exception:
            pass

    if not frames:
        raise FileNotFoundError(f"No parquet tables for TWAP market {market_id}")

    out = frames[0]
    for extra in frames[1:]:
        out = out.merge(extra, on="timestamp", how="outer")
    out = out.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    out["start_time"] = start
    out["end_time"] = end if end else start
    if open_f is not None:
        out["btc_open_price"] = open_f
    if winner_i is not None:
        out["winner"] = winner_i
    # Prefer Binance as btc_price; fall back to chainlink spot
    if "btc_price" not in out.columns and "btc_chainlink" in out.columns:
        out["btc_price"] = out["btc_chainlink"]
    return out


def live_market_summary(market_id: str) -> dict[str, Any] | None:
    d = find_live_market_dir(market_id)
    if d is None:
        return None
    meta = _read_meta(d / "meta.json") or {}
    try:
        start = int(meta.get("start_time") or 0)
    except (TypeError, ValueError):
        return None
    if start < _MIN_START_MS:
        return None
    try:
        end = int(meta.get("end_time") or 0)
    except (TypeError, ValueError):
        end = 0
    open_px = meta.get("btc_open_price")
    try:
        open_f = float(open_px) if open_px is not None else None
    except (TypeError, ValueError):
        open_f = None
    winner = meta.get("winner")
    if winner is True:
        winner_i: int | None = 1
    elif winner is False:
        winner_i = 0
    else:
        winner_i = None
    closed = bool(meta.get("closed")) or winner_i is not None
    rows = None
    try:
        df = load_live_market_frame(market_id)
        rows = int(len(df))
        if not end and not df.empty:
            end = int(df.iloc[-1]["timestamp"])
    except Exception:
        pass
    return {
        "market_id": str(meta.get("market_id") or market_id),
        "split": TWAP_SPLIT,
        "start_time": start,
        "end_time": end,
        "rows": rows,
        "winner": winner_i,
        "closed": closed,
        "btc_open_price": open_f,
        "has_features": False,
        "has_training": True,
    }
