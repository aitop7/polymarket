"""Download Polymarket L2 books + Chainlink feeds from PMData (https://pmdata.dev)."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from app.core.config import POLY_MONITOR_ROOT, settings

PMDATA_BASE = "https://api.pmdata.dev"
_CACHE_DIR = POLY_MONITOR_ROOT / "backend" / ".cache" / "pmdata"
_BLOCK_PATH = _CACHE_DIR / "blocked_until.json"
# Keep download parallelism low — PMData bans keys for "abnormal download activity".
_MAX_INFLIGHT = 2
_DOWNLOAD_SEM = threading.Semaphore(_MAX_INFLIGHT)
_BLOCK_LOCK = threading.Lock()
_blocked_until_ms: int | None = None

logger = logging.getLogger(__name__)


class PmDataBlockedError(RuntimeError):
    """Raised when PMData has temporarily blocked this API key."""

    def __init__(self, message: str, *, blocked_until_ms: int | None = None) -> None:
        super().__init__(message)
        self.blocked_until_ms = blocked_until_ms


def pmdata_enabled() -> bool:
    return bool((settings.pmdata_api_key or "").strip())


def _cache_path(slug: str, data_type: str = "poly_l2") -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in slug.strip())
    return _CACHE_DIR / f"{data_type}_{safe}.parquet"


def _api_key() -> str:
    key = (settings.pmdata_api_key or "").strip()
    if not key:
        raise RuntimeError("PMDATA_API_KEY is not configured")
    return key


def _parse_iso_ms(raw: str | None) -> int | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _format_until(ms: int) -> str:
    try:
        return (
            datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M %Z")
        )
    except (OSError, OverflowError, ValueError):
        return str(ms)


def _load_blocked_until_ms() -> int | None:
    global _blocked_until_ms
    with _BLOCK_LOCK:
        if _blocked_until_ms is not None:
            return _blocked_until_ms
        if not _BLOCK_PATH.is_file():
            return None
        try:
            raw = json.loads(_BLOCK_PATH.read_text(encoding="utf-8"))
            until = int(raw.get("blocked_until_ms") or 0)
        except Exception:
            return None
        if until <= 0:
            return None
        _blocked_until_ms = until
        return until


def _store_blocked_until_ms(until_ms: int) -> None:
    global _blocked_until_ms
    with _BLOCK_LOCK:
        _blocked_until_ms = int(until_ms)
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _BLOCK_PATH.write_text(
                json.dumps(
                    {
                        "blocked_until_ms": int(until_ms),
                        "updated_at": int(time.time() * 1000),
                    }
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Could not persist PMData block stamp: %s", exc)


def clear_pmdata_block() -> None:
    """Clear a stale local block stamp (e.g. after the ban window ends)."""
    global _blocked_until_ms
    with _BLOCK_LOCK:
        _blocked_until_ms = None
        try:
            if _BLOCK_PATH.is_file():
                _BLOCK_PATH.unlink()
        except OSError:
            pass


def pmdata_blocked_until_ms() -> int | None:
    until = _load_blocked_until_ms()
    if until is None:
        return None
    now = int(time.time() * 1000)
    if until <= now:
        clear_pmdata_block()
        return None
    return until


def assert_pmdata_not_blocked() -> None:
    until = pmdata_blocked_until_ms()
    if until is None:
        return
    raise PmDataBlockedError(
        f"PMData account temporarily blocked until {_format_until(until)} "
        f"(abnormal download activity). Books/Chainlink Generate paused.",
        blocked_until_ms=until,
    )


def _raise_http_error(status: int, body: str, reason: str) -> None:
    detail = (body or "").strip()[:500] or reason
    blocked_until_ms: int | None = None
    try:
        payload = json.loads(body)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        blocked_until_ms = _parse_iso_ms(
            str(payload.get("blocked_until") or payload.get("blockedUntil") or "")
        )
        err = str(payload.get("error") or "")
        msg = str(payload.get("message") or "")
        if blocked_until_ms is None and (
            "temporarily blocked" in err.lower()
            or "temporarily blocked" in msg.lower()
            or "abnormal download" in msg.lower()
        ):
            # Fallback: parse "... until 2026-08-17T12:18:54.266Z"
            for token in msg.replace(",", " ").split():
                if "T" in token and token[0].isdigit():
                    blocked_until_ms = _parse_iso_ms(token.strip("."))
                    if blocked_until_ms is not None:
                        break
        if blocked_until_ms is not None:
            _store_blocked_until_ms(blocked_until_ms)
            raise PmDataBlockedError(
                f"PMData account temporarily blocked until {_format_until(blocked_until_ms)}. "
                f"Stop Books/Chainlink Generate until then.",
                blocked_until_ms=blocked_until_ms,
            )
    if status in (401, 403):
        raise RuntimeError(f"PMData denied ({status}): {detail}")
    raise RuntimeError(f"PMData download failed ({status}): {detail}")


def _download_bytes(url: str, *, timeout_s: float = 180.0) -> bytes:
    assert_pmdata_not_blocked()
    headers = {
        "api_key": _api_key(),
        "User-Agent": "poly-monitor/pmdata",
        "Accept": "application/octet-stream,*/*",
    }
    with _DOWNLOAD_SEM:
        assert_pmdata_not_blocked()
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 404:
                raise FileNotFoundError(f"PMData file not found: {url}")
            if resp.status_code >= 400:
                _raise_http_error(
                    resp.status_code,
                    resp.text or "",
                    resp.reason_phrase or "",
                )
            return resp.content


def download_poly_l2(
    slug: str,
    *,
    force: bool = False,
    timeout_s: float = 180.0,
) -> pd.DataFrame:
    """
    Fetch PMData poly_l2 parquet for a market slug.

    Endpoint: GET https://api.pmdata.dev/download/poly_l2/{slug}.parquet
    Auth: api_key header (PMDATA_API_KEY).
    """
    slug = str(slug or "").strip()
    if not slug:
        raise ValueError("slug required")

    cache = _cache_path(slug)
    if cache.is_file() and not force:
        return pd.read_parquet(cache)

    url = f"{PMDATA_BASE}/download/poly_l2/{slug}.parquet"
    raw = _download_bytes(url, timeout_s=timeout_s)

    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".parquet.tmp")
    tmp.write_bytes(raw)
    tmp.replace(cache)
    return pd.read_parquet(cache)


def download_chainlink_day(
    date: str,
    *,
    data_type: str = "streams",
    symbol: str = "BTCUSD",
    force: bool = False,
    timeout_s: float = 300.0,
) -> pd.DataFrame:
    """
    Fetch a daily Chainlink parquet from PMData.

    Endpoint:
      GET https://api.pmdata.dev/chainlink/{symbol}/{data_type}/{symbol}_{data_type}_{date}.parquet
    data_type: streams | streams_twap30s | streams_twap60s
    """
    day = str(date or "").strip()
    if len(day) < 10:
        raise ValueError(f"invalid chainlink date: {date}")
    day = day[:10]
    dtype = str(data_type or "streams").strip()
    sym = str(symbol or "BTCUSD").strip().upper()
    fname = f"{sym}_{dtype}_{day}.parquet"
    cache = _CACHE_DIR / f"chainlink_{fname}"
    if cache.is_file() and not force:
        return pd.read_parquet(cache)

    url = f"{PMDATA_BASE}/chainlink/{sym}/{dtype}/{fname}"
    raw = _download_bytes(url, timeout_s=timeout_s)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".parquet.tmp")
    tmp.write_bytes(raw)
    tmp.replace(cache)
    return pd.read_parquet(cache)


def download_meta(slug: str) -> dict[str, Any]:
    """Lightweight probe: ensure API key works and file exists (uses cache)."""
    df = download_poly_l2(slug, force=False)
    return {"slug": slug, "n_rows": int(len(df)), "columns": list(df.columns)}
