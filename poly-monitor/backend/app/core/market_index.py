"""Cached lightweight market time index for calendar/time picking."""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from app.core.config import settings
from app.core.live_dataset import (
    TWAP_SPLIT,
    iter_live_market_metas,
    live_fingerprint,
)

SPLITS = ("train", "validation", "test")
ALL_SPLITS = (*SPLITS, TWAP_SPLIT)
ET = ZoneInfo("America/New_York")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_CACHE: dict[str, list[dict[str, Any]]] = {}


def _cache_dir() -> Path:
    d = Path(__file__).resolve().parents[3] / ".cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path(split: str) -> Path:
    return _cache_dir() / f"market_index_{split}.json"


def ms_to_et_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=ET).strftime("%Y-%m-%d")


def ms_to_et_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=ET).strftime("%H:%M")


def _split_dirs(split: str) -> tuple[Path, Path]:
    return settings.features_dir / split, settings.training_dir / split


def _list_ids(feat_dir: Path, train_dir: Path) -> list[str]:
    if feat_dir.is_dir():
        return sorted(p.stem for p in feat_dir.glob("*.parquet"))
    if train_dir.is_dir():
        return sorted(p.stem for p in train_dir.glob("*.parquet"))
    return []


def _fingerprint(feat_dir: Path, train_dir: Path, n_ids: int) -> dict[str, Any]:
    stamp = 0.0
    for d in (feat_dir, train_dir):
        if d.is_dir():
            stamp = max(stamp, d.stat().st_mtime)
    return {"n": n_ids, "stamp": stamp}


def _read_one(args: tuple[str, str, str, bool, bool]) -> dict[str, Any] | None:
    path_s, mid, split, feat_exists, train_exists = args
    path = Path(path_s)
    try:
        # Prefer start/end only — avoids full-column scan
        try:
            table = pq.read_table(path, columns=["start_time", "end_time"])
            start = int(table.column("start_time")[0].as_py())
            end = int(table.column("end_time")[0].as_py())
            nrows = int(table.num_rows)
        except Exception:
            table = pq.read_table(path, columns=["timestamp"])
            if table.num_rows == 0:
                return None
            start = int(table.column("timestamp")[0].as_py())
            end = int(table.column("timestamp")[-1].as_py())
            nrows = int(table.num_rows)
        return {
            "market_id": mid,
            "split": split,
            "start_time": start,
            "end_time": end,
            "date_et": ms_to_et_date(start),
            "time_et": ms_to_et_time(start),
            "has_features": feat_exists,
            "has_training": train_exists,
            "rows": nrows,
            "winner": None,
            "btc_open_price": None,
        }
    except Exception:
        return None


def invalidate_market_index(split: str | None = None) -> None:
    """Drop in-memory (+ optional on-disk) index so the next build rescans."""
    if split is None:
        _CACHE.clear()
        return
    _CACHE.pop(str(split), None)
    try:
        p = _cache_path(str(split))
        if p.is_file():
            p.unlink()
    except OSError:
        pass


def _build_twap_index(*, force: bool = False) -> list[dict[str, Any]]:
    cache_file = _cache_path(TWAP_SPLIT)
    fp = live_fingerprint()

    if not force and TWAP_SPLIT in _CACHE:
        # Re-validate against disk fingerprint so newly synced markets appear.
        try:
            if cache_file.is_file():
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                if int(payload.get("n", -1)) == fp["n"]:
                    return _CACHE[TWAP_SPLIT]
        except Exception:
            pass
        # Stale memory cache
        _CACHE.pop(TWAP_SPLIT, None)

    if not force and cache_file.is_file():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            if int(payload.get("n", -1)) == fp["n"] and isinstance(payload.get("markets"), list):
                rows = payload["markets"]
                _CACHE[TWAP_SPLIT] = rows
                return rows
        except Exception:
            pass

    rows = iter_live_market_metas()
    # Drop heavy path field from API/cache
    slim = [{k: v for k, v in r.items() if k != "dir"} for r in rows]
    try:
        cache_file.write_text(
            json.dumps({"n": fp["n"], "stamp": fp["stamp"], "markets": slim}, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception:
        pass
    _CACHE[TWAP_SPLIT] = slim
    return slim


def build_market_index(split: str, *, force: bool = False) -> list[dict[str, Any]]:
    if split == TWAP_SPLIT:
        return _build_twap_index(force=force)
    if split not in SPLITS:
        raise ValueError(f"Invalid split: {split}")

    if not force and split in _CACHE:
        return _CACHE[split]

    cache_file = _cache_path(split)
    feat_dir, train_dir = _split_dirs(split)
    ids = _list_ids(feat_dir, train_dir)
    fp = _fingerprint(feat_dir, train_dir, len(ids))

    if not force and cache_file.is_file():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            # Accept cache if market count matches (mtime of dirs is noisy on Windows)
            if int(payload.get("n", -1)) == fp["n"] and isinstance(payload.get("markets"), list):
                rows = payload["markets"]
                _CACHE[split] = rows
                return rows
        except Exception:
            pass

    jobs: list[tuple[str, str, str, bool, bool]] = []
    for mid in ids:
        train_path = train_dir / f"{mid}.parquet"
        feat_path = feat_dir / f"{mid}.parquet"
        path = train_path if train_path.is_file() else feat_path
        if not path.is_file():
            continue
        jobs.append((str(path), mid, split, feat_path.is_file(), train_path.is_file()))

    out: list[dict[str, Any]] = []
    workers = min(32, max(4, (len(jobs) // 200) or 8))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_read_one, job) for job in jobs]
        for fut in as_completed(futs):
            row = fut.result()
            if row:
                out.append(row)

    out.sort(key=lambda r: r["start_time"])
    try:
        cache_file.write_text(
            json.dumps({"n": fp["n"], "stamp": fp["stamp"], "markets": out}, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception:
        pass

    _CACHE[split] = out
    return out


def filter_history_markets(split: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For TWAP, hide the in-progress live window from the history picker."""
    if split != TWAP_SPLIT:
        return rows
    now_ms = int(time.time() * 1000)
    out: list[dict[str, Any]] = []
    for r in rows:
        start = int(r.get("start_time") or 0)
        end = int(r.get("end_time") or 0)
        # Only completed windows (ended); never the active live slot.
        if end <= 0 or end > now_ms:
            continue
        if start <= now_ms < end:
            continue
        out.append(r)
    return out


def list_dates(split: str) -> list[str]:
    idx = filter_history_markets(split, build_market_index(split))
    return sorted({r["date_et"] for r in idx})


MARKET_SLOT_S = 300
FULL_DAY_SLOTS = 24 * 60 // (MARKET_SLOT_S // 60)  # 288


def expected_slot_starts_s(
    date_et: str, *, now_ms: int | None = None
) -> list[int]:
    """Unix-second starts for every 5m BTC window on an ET calendar day.

    When ``now_ms`` is set, omit windows that have not finished yet (today).
    """
    if not _DAY_RE.match(str(date_et or "")):
        return []
    d0 = datetime.strptime(date_et, "%Y-%m-%d").replace(tzinfo=ET)
    d1 = d0 + timedelta(days=1)
    t0 = int(d0.timestamp())
    t1 = int(d1.timestamp())
    starts = list(range(t0, t1, MARKET_SLOT_S))
    if now_ms is not None:
        starts = [s for s in starts if (s + MARKET_SLOT_S) * 1000 <= int(now_ms)]
    return starts


def list_day_slot_gaps(
    date_et: str,
    *,
    split: str = TWAP_SPLIT,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Compare local history index vs expected 5m slots for one ET day."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    expected = expected_slot_starts_s(date_et, now_ms=now)
    day = list_markets_for_date(split, date_et) if expected else []
    have = {int(r.get("start_time") or 0) // 1000 for r in day}
    missing: list[dict[str, Any]] = []
    for start_s in expected:
        if start_s in have:
            continue
        missing.append(
            {
                "start_s": start_s,
                "start_time": start_s * 1000,
                "end_time": (start_s + MARKET_SLOT_S) * 1000,
                "slug": f"btc-updown-5m-{start_s}",
                "time_et": ms_to_et_time(start_s * 1000),
            }
        )
    day_start_ms = expected[0] * 1000 if expected else 0
    day_end_ms = (expected[-1] + MARKET_SLOT_S) * 1000 if expected else 0
    return {
        "date_et": date_et,
        "split": split,
        "expected": len(expected),
        "present": len(day),
        "n_missing": len(missing),
        "missing": missing,
        "day_start_ms": day_start_ms,
        "day_end_ms": day_end_ms,
        "full_day_slots": FULL_DAY_SLOTS,
    }


def list_markets_for_date(split: str, date_et: str) -> list[dict[str, Any]]:
    idx = filter_history_markets(split, build_market_index(split))
    day = [r for r in idx if r["date_et"] == date_et]
    day.sort(key=lambda r: int(r["start_time"]), reverse=True)
    return day


def find_market_at(split: str, timestamp_ms: int) -> dict[str, Any] | None:
    """Return the market whose window contains ``timestamp_ms``.

    Does **not** fall back to an arbitrary nearest market — that caused the
    wallet chart to show a different 5m slot than the selected Activity row.
    """
    idx = build_market_index(split)
    if not idx:
        return None
    t = int(timestamp_ms)
    for r in idx:
        try:
            start = int(r["start_time"])
            end = int(r["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if start <= t < end:
            return r
    # Tiny skew only (slug open vs parquet start), still same 5m slot.
    best: dict[str, Any] | None = None
    best_abs: int | None = None
    for r in idx:
        try:
            start = int(r["start_time"])
        except (KeyError, TypeError, ValueError):
            continue
        d = abs(start - t)
        if d <= 60_000 and (best_abs is None or d < best_abs):
            best = r
            best_abs = d
    return best


def find_market_by_date_time(split: str, date_et: str, time_et: str) -> dict[str, Any] | None:
    day = list_markets_for_date(split, date_et)
    if not day:
        return None
    for r in day:
        if r["time_et"] == time_et:
            return r
    try:
        hh, mm = map(int, time_et.split(":"))
        target_min = hh * 60 + mm
    except Exception:
        return None

    def mins(r: dict[str, Any]) -> int:
        h, m = map(int, r["time_et"].split(":"))
        return h * 60 + m

    # Clock rounding only — never jump to a distant slot on the same day.
    best = min(day, key=lambda r: abs(mins(r) - target_min))
    if abs(mins(best) - target_min) <= 2:
        return best
    return None
