"""Build pm_chainlink_price.parquet from PMData Chainlink streams (0.5s grid)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.core.live_dataset import find_live_market_dir
from app.core.pmdata_client import download_chainlink_day, pmdata_enabled
from app.core.series import get_series, series_from_slug, series_key_from_meta

SLOT_MS = 500
PREMARKET_LEAD_MS = 300_000
PM_CHAINLINK_FILE = "pm_chainlink_price.parquet"
CHAINLINK_COLUMNS = ["timestamp", "Chainlink_BTC", "twap"]
_SCALE = Decimal(10) ** 18


def _read_meta(market_dir: Path) -> dict[str, Any]:
    path = market_dir / "meta.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"invalid meta.json: {path}")
    return raw


def _to_ms(ts: Any) -> int | None:
    if ts is None or (isinstance(ts, float) and not np.isfinite(ts)):
        return None
    if isinstance(ts, pd.Timestamp):
        if pd.isna(ts):
            return None
        return int(ts.value // 1_000_000)
    if isinstance(ts, np.datetime64):
        if np.isnat(ts):
            return None
        return int(ts.astype("datetime64[ms]").astype(np.int64))
    if hasattr(ts, "timestamp") and callable(ts.timestamp):
        try:
            return int(round(float(ts.timestamp()) * 1000.0))
        except (OSError, OverflowError, ValueError, TypeError):
            pass
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return None
    if v >= 10_000_000_000_000_000:
        return v // 1_000_000
    if v >= 10_000_000_000_000:
        return v // 1000
    if v >= 1_000_000_000_000:
        return v
    if v >= 1_000_000_000:
        return v * 1000
    return None


def _series_to_ms(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        as_ns = series.astype("datetime64[ns]").astype("int64")
        out = (as_ns // 1_000_000).astype("Int64")
        nat = series.isna()
        if nat.any():
            out = out.mask(nat)
        return out
    return series.map(_to_ms).astype("Int64")


def _scale_price(raw: Any) -> float | None:
    if raw is None or (isinstance(raw, float) and not np.isfinite(raw)):
        return None
    s = str(raw).strip()
    if not s or s.lower() in {"none", "nan", "null"}:
        return None
    try:
        return float(Decimal(s) / _SCALE)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _utc_dates_for_window(start_ms: int, end_ms: int, *, pad_ms: int = 60_000) -> list[str]:
    t0 = datetime.fromtimestamp(max(0, start_ms - pad_ms) / 1000.0, tz=timezone.utc).date()
    t1 = datetime.fromtimestamp(max(start_ms, end_ms) / 1000.0, tz=timezone.utc).date()
    out: list[str] = []
    d = t0
    while d <= t1:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _load_feed_days(
    dates: list[str],
    *,
    data_type: str,
    force: bool,
    symbol: str = "BTCUSD",
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for day in dates:
        try:
            frames.append(
                download_chainlink_day(
                    day, data_type=data_type, force=force, symbol=symbol
                )
            )
        except FileNotFoundError:
            continue
    if not frames:
        raise FileNotFoundError(
            f"PMData chainlink {data_type} missing for dates={dates} symbol={symbol}"
        )
    return pd.concat(frames, ignore_index=True)


def _prep_price_series(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(columns=["_ts_ms", "price"])
    ts_col = "observationsTimestamp" if "observationsTimestamp" in raw.columns else None
    if ts_col is None and "receiveMicrosecondTimestamp" in raw.columns:
        ts_col = "receiveMicrosecondTimestamp"
    if ts_col is None:
        raise RuntimeError(f"chainlink file missing timestamp columns: {list(raw.columns)}")
    if "price" not in raw.columns:
        raise RuntimeError(f"chainlink file missing price column: {list(raw.columns)}")

    df = pd.DataFrame(
        {
            "_ts_ms": _series_to_ms(raw[ts_col]),
            "price": raw["price"].map(_scale_price),
        }
    )
    df = df.dropna(subset=["_ts_ms", "price"]).sort_values("_ts_ms", kind="mergesort")
    df = df.drop_duplicates(subset=["_ts_ms"], keep="last").reset_index(drop=True)
    return df


def _asof_values(ts_ms: np.ndarray, values: np.ndarray, grid: list[int]) -> list[float | None]:
    """Last observation at or before each grid timestamp."""
    out: list[float | None] = []
    i = 0
    n = len(ts_ms)
    last: float | None = None
    for t in grid:
        while i < n and int(ts_ms[i]) <= t:
            last = float(values[i])
            i += 1
        out.append(last)
    return out


def generate_pm_chainlink_for_market(
    market_id: str,
    *,
    force_download: bool = False,
    slot_ms: int = SLOT_MS,
) -> dict[str, Any]:
    if not pmdata_enabled("chainlink"):
        raise RuntimeError(
            "PMDATA_API_KEY_CHAINLINK (or PMDATA_API_KEY) is not configured"
        )

    mid = str(market_id).strip()
    market_dir = find_live_market_dir(mid)
    if market_dir is None:
        raise FileNotFoundError(f"Live market not found under FETCH_LIVE_DATA_DIR: {mid}")

    meta = _read_meta(market_dir)
    try:
        start_ms = int(meta.get("start_time") or 0)
        end_ms = int(meta.get("end_time") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"meta.json missing start/end for market {mid}") from exc
    if start_ms <= 0 or end_ms <= start_ms:
        raise RuntimeError(f"invalid market window for {mid}: {start_ms}-{end_ms}")

    dates = _utc_dates_for_window(start_ms, end_ms, pad_ms=PREMARKET_LEAD_MS)
    hit = series_from_slug(str(meta.get("slug") or ""))
    if hit is None:
        hit = get_series(series_key_from_meta(meta))
    cl_symbol = hit.chainlink_symbol
    spot_raw = _load_feed_days(
        dates, data_type="streams", force=force_download, symbol=cl_symbol
    )
    # BTC 5m markets settle on 60s TWAP; fall back to 30s for older PMData days.
    try:
        twap_raw = _load_feed_days(
            dates,
            data_type="streams_twap60s",
            force=force_download,
            symbol=cl_symbol,
        )
        twap_source = "streams_twap60s"
    except FileNotFoundError:
        twap_raw = _load_feed_days(
            dates,
            data_type="streams_twap30s",
            force=force_download,
            symbol=cl_symbol,
        )
        twap_source = "streams_twap30s"
    spot = _prep_price_series(spot_raw)
    twap = _prep_price_series(twap_raw)
    if spot.empty:
        raise RuntimeError(f"PMData chainlink streams empty for dates={dates}")

    slot = max(100, int(slot_ms))
    # Include lead-in when Chainlink ticks exist before market open.
    lead_floor = start_ms - PREMARKET_LEAD_MS
    spot_ts_all = spot["_ts_ms"].to_numpy(dtype=np.int64, copy=False)
    pre_idx = int(np.searchsorted(spot_ts_all, lead_floor, side="left"))
    grid_start = start_ms
    if pre_idx < len(spot_ts_all) and int(spot_ts_all[pre_idx]) < start_ms:
        grid_start = max(lead_floor, int(spot_ts_all[pre_idx]))

    t0 = grid_start - (grid_start % slot)
    if t0 < grid_start:
        t0 += slot
    grid = list(range(t0, end_ms + 1, slot))
    if not grid:
        grid = [start_ms]

    # Warm as-of state with ticks before the first grid point.
    spot_ts = spot_ts_all
    spot_px = spot["price"].to_numpy(dtype=np.float64, copy=False)
    twap_ts = twap["_ts_ms"].to_numpy(dtype=np.int64, copy=False) if not twap.empty else np.array([], dtype=np.int64)
    twap_px = twap["price"].to_numpy(dtype=np.float64, copy=False) if not twap.empty else np.array([], dtype=np.float64)

    spot_vals = _asof_values(spot_ts, spot_px, grid)
    twap_vals = _asof_values(twap_ts, twap_px, grid) if len(twap_ts) else [None] * len(grid)

    rows = [
        {
            "timestamp": int(t),
            "Chainlink_BTC": spot_vals[i],
            "twap": twap_vals[i],
        }
        for i, t in enumerate(grid)
    ]
    if not rows:
        raise RuntimeError("no pm_chainlink_price rows generated")

    out_df = pd.DataFrame(rows, columns=CHAINLINK_COLUMNS)
    out_df["timestamp"] = out_df["timestamp"].astype("int64")
    out_df["Chainlink_BTC"] = pd.to_numeric(out_df["Chainlink_BTC"], errors="coerce").astype("float32")
    out_df["twap"] = pd.to_numeric(out_df["twap"], errors="coerce").astype("float32")

    out_path = market_dir / PM_CHAINLINK_FILE
    tmp = out_path.with_suffix(".parquet.tmp")
    out_df.to_parquet(tmp, index=False)
    tmp.replace(out_path)

    warning = None
    if out_df["twap"].isna().all():
        warning = f"PMData {twap_source} had no usable values — twap column is null"
    elif out_df["Chainlink_BTC"].isna().any():
        warning = "Some 0.5s slots had no prior Chainlink spot tick"

    return {
        "ok": True,
        "market_id": mid,
        "slug": meta.get("slug"),
        "path": str(out_path),
        "n_rows": int(len(out_df)),
        "slot_ms": slot,
        "source": "pmdata",
        "dates": dates,
        "start_time": start_ms,
        "end_time": end_ms,
        "grid_start": int(out_df["timestamp"].iloc[0]) if len(out_df) else start_ms,
        "premarket_ms": max(0, start_ms - int(out_df["timestamp"].iloc[0])) if len(out_df) else 0,
        "warning": warning,
    }


def has_pm_chainlink(market_id: str) -> bool:
    d = find_live_market_dir(str(market_id).strip())
    return bool(d and (d / PM_CHAINLINK_FILE).is_file())


def list_missing_pm_chainlink(*, date_et: str | None = None) -> dict[str, Any]:
    """History markets missing pm_chainlink_price.parquet (oldest → newest)."""
    from app.core.live_dataset import TWAP_SPLIT
    from app.core.market_index import (
        build_market_index,
        filter_history_markets,
        list_markets_for_date,
    )

    date = (date_et or "").strip() or None
    if date:
        rows = list_markets_for_date(TWAP_SPLIT, date)
    else:
        rows = filter_history_markets(TWAP_SPLIT, build_market_index(TWAP_SPLIT))

    missing: list[dict[str, Any]] = []
    present = 0
    for r in rows:
        mid = str(r.get("market_id") or "")
        if not mid:
            continue
        d = Path(str(r["dir"])) if r.get("dir") else find_live_market_dir(mid)
        ok = bool(d and (d / PM_CHAINLINK_FILE).is_file())
        if ok:
            present += 1
            continue
        missing.append(
            {
                "market_id": mid,
                "slug": None,
                "series": None,
                "start_time": int(r.get("start_time") or 0),
                "end_time": int(r.get("end_time") or 0),
                "date_et": r.get("date_et"),
                "time_et": r.get("time_et"),
                "dir": str(d) if d else None,
            }
        )

    missing.sort(key=lambda r: (int(r.get("start_time") or 0), str(r.get("market_id") or "")))

    for item in missing:
        d = Path(item["dir"]) if item.get("dir") else None
        if d is None:
            continue
        try:
            meta = _read_meta(d)
            item["slug"] = meta.get("slug")
            series = meta.get("series")
            if series not in ("5m", "15m"):
                from app.core.series import series_from_slug

                hit = series_from_slug(str(meta.get("slug") or ""))
                series = hit.key if hit else None
            item["series"] = series
        except Exception:
            pass

    from app.core.pmdata_client import pmdata_blocked_until_ms, pmdata_enabled

    blocked_until = pmdata_blocked_until_ms("chainlink") if pmdata_enabled("chainlink") else None
    return {
        "date": date,
        "n_total": present + len(missing),
        "n_present": present,
        "n_missing": len(missing),
        "missing": missing,
        "pmdata_enabled": pmdata_enabled("chainlink"),
        "pmdata_blocked_until_ms": blocked_until,
    }
