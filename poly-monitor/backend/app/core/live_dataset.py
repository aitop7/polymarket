"""Index and load markets from FETCH_LIVE_DATA_DIR (TWAP / live parquet tree)."""

from __future__ import annotations

import json
import re
import time
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

# Book tables under each fetch_live market dir.
# pm_orderbooks.parquet is preferred; orderbooks.parquet is the fallback capture.
ORDERBOOKS_FILE = "orderbooks.parquet"
PM_ORDERBOOKS_FILE = "pm_orderbooks.parquet"
CHAINLINK_FILE = "chainlink_price.parquet"
PM_CHAINLINK_FILE = "pm_chainlink_price.parquet"


def resolve_orderbooks_path(market_dir: Path | str | None) -> Path | None:
    """Prefer non-empty pm_orderbooks.parquet; fall back to orderbooks.parquet."""
    if market_dir is None:
        return None
    d = Path(market_dir)
    if not d.is_dir():
        return None
    pm = d / PM_ORDERBOOKS_FILE
    try:
        if pm.is_file() and pm.stat().st_size > 0:
            return pm
    except OSError:
        pass
    ob = d / ORDERBOOKS_FILE
    try:
        if ob.is_file() and ob.stat().st_size > 0:
            return ob
    except OSError:
        pass
    return None


def resolve_chainlink_path(market_dir: Path | str | None) -> Path | None:
    """Prefer non-empty pm_chainlink_price.parquet; fall back to chainlink_price.parquet."""
    if market_dir is None:
        return None
    d = Path(market_dir)
    if not d.is_dir():
        return None
    pm = d / PM_CHAINLINK_FILE
    try:
        if pm.is_file() and pm.stat().st_size > 0:
            return pm
    except OSError:
        pass
    cl = d / CHAINLINK_FILE
    try:
        if cl.is_file() and cl.stat().st_size > 0:
            return cl
    except OSError:
        pass
    return None


# Persisted on meta.json after first history integrity check (gap severity).
DATA_HEALTH_GREAT = "great"
DATA_HEALTH_GOOD = "good"
DATA_HEALTH_OK = "ok"
DATA_HEALTH_LOW = "low"
DATA_HEALTH_BAD = "bad"
DATA_HEALTH_UNCHECKED = "unchecked"
DATA_HEALTH_CHECKED = frozenset(
    {
        DATA_HEALTH_GREAT,
        DATA_HEALTH_GOOD,
        DATA_HEALTH_OK,
        DATA_HEALTH_LOW,
        DATA_HEALTH_BAD,
    }
)
_DATA_HEALTH_RANK = {
    DATA_HEALTH_GREAT: 0,
    DATA_HEALTH_GOOD: 1,
    DATA_HEALTH_OK: 2,
    DATA_HEALTH_LOW: 3,
    DATA_HEALTH_BAD: 4,
}
# Legacy aliases kept for older meta.json values.
DATA_HEALTH_HEALTHY = DATA_HEALTH_GREAT
DATA_HEALTH_UNHEALTHY = DATA_HEALTH_BAD

# Thresholds: poly-monitor/shared/data_health.json (single source for FE + BE).
_POLY_MONITOR_ROOT = Path(__file__).resolve().parents[3]
_DATA_HEALTH_JSON = _POLY_MONITOR_ROOT / "shared" / "data_health.json"


def _load_data_health_thresholds() -> dict[str, Any]:
    try:
        raw = json.loads(_DATA_HEALTH_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed to load {_DATA_HEALTH_JSON}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"Invalid data_health.json root (expected object)")
    price = raw.get("price") if isinstance(raw.get("price"), dict) else {}
    trade = raw.get("trade") if isinstance(raw.get("trade"), dict) else {}
    return {"price": price, "trade": trade}


_DATA_HEALTH_THRESHOLDS = _load_data_health_thresholds()


def _threshold_ms(section: str, key: str, default: int) -> int:
    try:
        return int((_DATA_HEALTH_THRESHOLDS.get(section) or {}).get(key, default))
    except (TypeError, ValueError):
        return int(default)


# Price/orderbook 1s series: inclusive upper bounds (gap <= X → tier; above Low → Bad)
PRICE_HEALTH_GREAT_MS = _threshold_ms("price", "great_ms", 2_000)
PRICE_HEALTH_GOOD_MS = _threshold_ms("price", "good_ms", 3_000)
PRICE_HEALTH_OK_MS = _threshold_ms("price", "ok_ms", 5_000)
PRICE_HEALTH_LOW_MS = _threshold_ms("price", "low_ms", 10_000)

# Trade tapes: exclusive upper bounds (quiet < X → tier; >= Low → Bad)
TRADE_HEALTH_GREAT_MS = _threshold_ms("trade", "great_ms", 5_000)
TRADE_HEALTH_GOOD_MS = _threshold_ms("trade", "good_ms", 10_000)
TRADE_HEALTH_OK_MS = _threshold_ms("trade", "ok_ms", 15_000)
TRADE_HEALTH_LOW_MS = _threshold_ms("trade", "low_ms", 20_000)

# Derived helpers used by gap scanners / repair.
PRICE_SERIES_STEP_MS = PRICE_HEALTH_GREAT_MS  # expected cadence from Great bound
TRADE_NOTE_MS = TRADE_HEALTH_GREAT_MS  # include quiets at/above Great boundary in comments
TRADE_REPAIR_MS = TRADE_HEALTH_LOW_MS  # force repair / backfill when trade grade is Bad


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


def grade_data_health(max_gap_ms: int) -> str:
    """1s price/orderbook: worst single gap → Great→Bad (never a sum)."""
    gap = max(0, int(max_gap_ms))
    if gap <= PRICE_HEALTH_GREAT_MS:
        return DATA_HEALTH_GREAT
    if gap <= PRICE_HEALTH_GOOD_MS:
        return DATA_HEALTH_GOOD
    if gap <= PRICE_HEALTH_OK_MS:
        return DATA_HEALTH_OK
    if gap <= PRICE_HEALTH_LOW_MS:
        return DATA_HEALTH_LOW
    return DATA_HEALTH_BAD


def grade_trade_health(max_quiet_ms: int) -> str:
    """Trade tapes: worst single quiet → Great→Bad (never a sum)."""
    quiet = max(0, int(max_quiet_ms))
    if quiet < TRADE_HEALTH_GREAT_MS:
        return DATA_HEALTH_GREAT
    if quiet < TRADE_HEALTH_GOOD_MS:
        return DATA_HEALTH_GOOD
    if quiet < TRADE_HEALTH_OK_MS:
        return DATA_HEALTH_OK
    if quiet < TRADE_HEALTH_LOW_MS:
        return DATA_HEALTH_LOW
    return DATA_HEALTH_BAD


def data_health_thresholds() -> dict[str, Any]:
    """Public snapshot of tunable thresholds (ms) from shared/data_health.json."""
    price = _DATA_HEALTH_THRESHOLDS.get("price") or {}
    trade = _DATA_HEALTH_THRESHOLDS.get("trade") or {}
    return {
        "price": {
            "great_ms": PRICE_HEALTH_GREAT_MS,
            "good_ms": PRICE_HEALTH_GOOD_MS,
            "ok_ms": PRICE_HEALTH_OK_MS,
            "low_ms": PRICE_HEALTH_LOW_MS,
            "inclusive": bool(price.get("inclusive", True)),
        },
        "trade": {
            "great_ms": TRADE_HEALTH_GREAT_MS,
            "good_ms": TRADE_HEALTH_GOOD_MS,
            "ok_ms": TRADE_HEALTH_OK_MS,
            "low_ms": TRADE_HEALTH_LOW_MS,
            "inclusive": bool(trade.get("inclusive", False)),
        },
        "source": str(_DATA_HEALTH_JSON),
    }


def worse_data_health(*grades: Any) -> str:
    """Return the more severe Great→Bad grade among inputs."""
    best = DATA_HEALTH_GREAT
    best_rank = _DATA_HEALTH_RANK[DATA_HEALTH_GREAT]
    for raw in grades:
        g = normalize_data_health(raw)
        if g not in _DATA_HEALTH_RANK:
            continue
        rank = _DATA_HEALTH_RANK[g]
        if rank > best_rank:
            best = g
            best_rank = rank
    return best


def normalize_data_health(value: Any) -> str:
    s = str(value or "").strip().lower().replace("-", "_")
    if s in {DATA_HEALTH_GREAT, "healthy", "perfect"}:
        return DATA_HEALTH_GREAT
    if s == DATA_HEALTH_GOOD:
        return DATA_HEALTH_GOOD
    if s == DATA_HEALTH_OK:
        return DATA_HEALTH_OK
    if s == DATA_HEALTH_LOW:
        return DATA_HEALTH_LOW
    if s in {DATA_HEALTH_BAD, "unhealthy", "not_healthy", "gap", "gappy"}:
        return DATA_HEALTH_BAD
    return DATA_HEALTH_UNCHECKED


def read_data_health(meta: dict[str, Any] | None) -> str:
    if not meta:
        return DATA_HEALTH_UNCHECKED
    return normalize_data_health(meta.get("data_health"))


def is_data_health_checked(value: Any) -> bool:
    return normalize_data_health(value) in DATA_HEALTH_CHECKED


def read_data_health_comment(meta: dict[str, Any] | None) -> str | None:
    if not meta:
        return None
    text = str(meta.get("data_health_comment") or "").strip()
    return text or None


def write_data_health(
    market_dir: Path,
    health: str,
    *,
    comment: str | None = None,
    checked_at_ms: int | None = None,
    orderbooks_source: str | None = None,
    chainlink_source: str | None = None,
) -> str:
    """Persist data_health (+ optional gap comment / PM source stamps) on meta.json."""
    status = normalize_data_health(health)
    if status == DATA_HEALTH_UNCHECKED:
        status = DATA_HEALTH_BAD
    meta_path = market_dir / "meta.json"
    meta = _read_meta(meta_path) or {}
    meta["data_health"] = status
    meta["data_health_checked_at"] = int(
        checked_at_ms if checked_at_ms is not None else time.time() * 1000
    )
    note = str(comment or "").strip()
    if status == DATA_HEALTH_GREAT:
        meta["data_health_comment"] = ""
    elif note:
        meta["data_health_comment"] = note
    else:
        meta.setdefault("data_health_comment", "")
    if orderbooks_source is not None:
        meta["data_health_orderbooks_source"] = str(orderbooks_source or "")
    if chainlink_source is not None:
        meta["data_health_chainlink_source"] = str(chainlink_source or "")
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    tmp.replace(meta_path)
    return status


# Tape / Binance Fix stamps — when set, panel + history gap scoring skips that file.
META_TRADES_CHECKED = "trades_repaired_complete"
META_BINANCE_PRICE_CHECKED = "binance_price_checked"
META_BINANCE_TRADES_CHECKED = "binance_trades_checked"


def stamp_tape_checked(
    market_dir: Path,
    *flags: str,
    checked_at_ms: int | None = None,
) -> dict[str, Any]:
    """Set one or more Fix-checked flags on meta.json (atomic rewrite)."""
    meta_path = market_dir / "meta.json"
    meta = _read_meta(meta_path) or {}
    now = int(checked_at_ms if checked_at_ms is not None else time.time() * 1000)
    for raw in flags:
        key = str(raw or "").strip()
        if not key:
            continue
        meta[key] = True
        meta[f"{key}_at"] = now
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    tmp.replace(meta_path)
    return meta


def _nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def needs_pmdata_health_rescore(market_dir: Path, meta: dict[str, Any] | None = None) -> bool:
    """
    True when PM books/chainlink exist but meta health was not stamped from those files.
    """
    d = Path(market_dir)
    info = meta if isinstance(meta, dict) else (_read_meta(d / "meta.json") or {})
    has_ob = _nonempty_file(d / PM_ORDERBOOKS_FILE)
    has_cl = _nonempty_file(d / PM_CHAINLINK_FILE)
    if not has_ob and not has_cl:
        return False
    stored_ob = str(info.get("data_health_orderbooks_source") or "").strip()
    stored_cl = str(info.get("data_health_chainlink_source") or "").strip()
    if has_ob and stored_ob != PM_ORDERBOOKS_FILE:
        return True
    if has_cl and stored_cl != PM_CHAINLINK_FILE:
        return True
    return False


def list_pmdata_health_rescore_queue(*, date_et: str | None = None) -> dict[str, Any]:
    """History markets with PM files whose health stamp is not yet from PMData."""
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

    queued: list[dict[str, Any]] = []
    present = 0
    for r in rows:
        mid = str(r.get("market_id") or "")
        if not mid:
            continue
        d = Path(str(r["dir"])) if r.get("dir") else find_live_market_dir(mid)
        if d is None or not d.is_dir():
            continue
        has_pm = _nonempty_file(d / PM_ORDERBOOKS_FILE) or _nonempty_file(d / PM_CHAINLINK_FILE)
        if not has_pm:
            continue
        meta = _read_meta(d / "meta.json") or {}
        if not needs_pmdata_health_rescore(d, meta):
            present += 1
            continue
        reasons: list[str] = []
        if _nonempty_file(d / PM_ORDERBOOKS_FILE) and str(
            meta.get("data_health_orderbooks_source") or ""
        ).strip() != PM_ORDERBOOKS_FILE:
            reasons.append("books")
        if _nonempty_file(d / PM_CHAINLINK_FILE) and str(
            meta.get("data_health_chainlink_source") or ""
        ).strip() != PM_CHAINLINK_FILE:
            reasons.append("chainlink")
        queued.append(
            {
                "market_id": mid,
                "slug": meta.get("slug"),
                "start_time": int(r.get("start_time") or meta.get("start_time") or 0),
                "end_time": int(r.get("end_time") or meta.get("end_time") or 0),
                "date_et": r.get("date_et"),
                "time_et": r.get("time_et"),
                "reasons": reasons,
                "data_health": read_data_health(meta),
            }
        )

    queued.sort(key=lambda row: (int(row.get("start_time") or 0), str(row.get("market_id") or "")))
    return {
        "date": date,
        "n_total": present + len(queued),
        "n_present": present,
        "n_missing": len(queued),
        "missing": queued,
    }


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
            try:
                resolved_at = int(meta["resolved_at"]) if meta.get("resolved_at") is not None else None
            except (TypeError, ValueError):
                resolved_at = None
            out.append(
                {
                    "market_id": mid,
                    "split": TWAP_SPLIT,
                    "slug": str(meta.get("slug") or "") or None,
                    "series": (
                        str(meta.get("series") or "").strip().lower()
                        if str(meta.get("series") or "").strip().lower() in {"5m", "15m"}
                        else None
                    ),
                    "start_time": start,
                    "end_time": end,
                    "date_et": _ms_to_et_date(start),
                    "time_et": _ms_to_et_time(start),
                    "has_features": False,
                    "has_training": True,
                    "rows": None,
                    "winner": winner_i,
                    "closed": closed,
                    "resolved_at": resolved_at,
                    "btc_open_price": open_f,
                    "data_health": read_data_health(meta),
                    "data_health_comment": read_data_health_comment(meta),
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
    cl = resolve_chainlink_path(d)
    if cl is not None:
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
                # Retain depth bands as well as the mid; short-horizon models
                # derive Binance order-book imbalance from these columns.
                frames.append(df.rename(columns={"Binance_BTC": "btc_price"}))
        except Exception:
            pass

    ob = resolve_orderbooks_path(d)
    if ob is not None:
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
    try:
        resolved_at = int(meta["resolved_at"]) if meta.get("resolved_at") is not None else None
    except (TypeError, ValueError):
        resolved_at = None
    rows = None
    orderbooks_source = None
    chainlink_source = None
    try:
        ob = resolve_orderbooks_path(d)
        orderbooks_source = ob.name if ob is not None else None
        cl = resolve_chainlink_path(d)
        chainlink_source = cl.name if cl is not None else None
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
        "resolved_at": resolved_at,
        "btc_open_price": open_f,
        "data_health": read_data_health(meta),
        "data_health_comment": read_data_health_comment(meta),
        "orderbooks_source": orderbooks_source,
        "chainlink_source": chainlink_source,
        "has_features": False,
        "has_training": True,
    }
