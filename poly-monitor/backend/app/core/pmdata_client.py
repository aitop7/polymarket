"""Download Polymarket L2 books + Chainlink feeds from PMData (https://pmdata.dev)."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

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
# key fingerprint -> blocked_until_ms
_blocked_until: dict[str, int] = {}

logger = logging.getLogger(__name__)

PmDataPurpose = Literal["books", "chainlink"]


class PmDataBlockedError(RuntimeError):
    """Raised when PMData has temporarily blocked this API key."""

    def __init__(
        self,
        message: str,
        *,
        blocked_until_ms: int | None = None,
        purpose: str | None = None,
    ) -> None:
        super().__init__(message)
        self.blocked_until_ms = blocked_until_ms
        self.purpose = purpose


def pmdata_enabled(purpose: PmDataPurpose | None = None) -> bool:
    """True when the shared key or the purpose-specific key is set."""
    if purpose == "books":
        return bool(_resolve_api_key("books", required=False))
    if purpose == "chainlink":
        return bool(_resolve_api_key("chainlink", required=False))
    return bool(
        _resolve_api_key("books", required=False)
        or _resolve_api_key("chainlink", required=False)
    )


def _cache_path(slug: str, data_type: str = "poly_l2") -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in slug.strip())
    return _CACHE_DIR / f"{data_type}_{safe}.parquet"


def _resolve_api_key(purpose: PmDataPurpose, *, required: bool = True) -> str:
    """
    Prefer purpose-specific env keys; fall back to shared PMDATA_API_KEY.

      PMDATA_API_KEY_BOOKS / PMDATA_API_KEY_CHAINLINK
      PMDATA_API_KEY (legacy shared fallback)
    """
    if purpose == "books":
        key = (settings.pmdata_api_key_books or "").strip()
        label = "PMDATA_API_KEY_BOOKS"
    else:
        key = (settings.pmdata_api_key_chainlink or "").strip()
        label = "PMDATA_API_KEY_CHAINLINK"
    if not key:
        key = (settings.pmdata_api_key or "").strip()
        label = f"{label} (or PMDATA_API_KEY)"
    if not key and required:
        raise RuntimeError(f"{label} is not configured")
    return key


def _key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()[:16]


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


def _read_block_file() -> dict[str, int]:
    if not _BLOCK_PATH.is_file():
        return {}
    try:
        raw = json.loads(_BLOCK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, int] = {}
    # New format: { "blocks": { fp: until_ms } }
    blocks = raw.get("blocks") if isinstance(raw, dict) else None
    if isinstance(blocks, dict):
        for fp, until in blocks.items():
            try:
                ms = int(until)
            except (TypeError, ValueError):
                continue
            if ms > 0:
                out[str(fp)] = ms
        return out
    # Legacy single-stamp format
    if isinstance(raw, dict):
        try:
            until = int(raw.get("blocked_until_ms") or 0)
        except (TypeError, ValueError):
            until = 0
        fp = str(raw.get("key_fp") or "")
        if until > 0 and fp:
            out[fp] = until
    return out


def _write_block_file(blocks: dict[str, int]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        now = int(time.time() * 1000)
        # Drop expired entries
        live = {fp: ms for fp, ms in blocks.items() if ms > now}
        if not live:
            if _BLOCK_PATH.is_file():
                _BLOCK_PATH.unlink()
            return
        _BLOCK_PATH.write_text(
            json.dumps({"blocks": live, "updated_at": now}),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not persist PMData block stamp: %s", exc)


def _load_blocked_until_ms(key: str) -> int | None:
    global _blocked_until
    fp = _key_fingerprint(key)
    now = int(time.time() * 1000)
    with _BLOCK_LOCK:
        until = _blocked_until.get(fp)
        if until is None:
            disk = _read_block_file()
            _blocked_until = disk
            until = _blocked_until.get(fp)
        if until is None:
            return None
        if until <= now:
            _blocked_until.pop(fp, None)
            _write_block_file(_blocked_until)
            return None
        return until


def _store_blocked_until_ms(key: str, until_ms: int) -> None:
    global _blocked_until
    fp = _key_fingerprint(key)
    with _BLOCK_LOCK:
        _blocked_until[fp] = int(until_ms)
        _write_block_file(_blocked_until)


def clear_pmdata_block(*, purpose: PmDataPurpose | None = None) -> None:
    """Clear local block stamp(s). If purpose is set, only that key's stamp."""
    global _blocked_until
    with _BLOCK_LOCK:
        if purpose is None:
            _blocked_until = {}
            try:
                if _BLOCK_PATH.is_file():
                    _BLOCK_PATH.unlink()
            except OSError:
                pass
            return
        key = _resolve_api_key(purpose, required=False)
        if not key:
            return
        fp = _key_fingerprint(key)
        _blocked_until.pop(fp, None)
        _write_block_file(_blocked_until)


def pmdata_blocked_until_ms(purpose: PmDataPurpose = "books") -> int | None:
    key = _resolve_api_key(purpose, required=False)
    if not key:
        return None
    return _load_blocked_until_ms(key)


def assert_pmdata_not_blocked(purpose: PmDataPurpose, *, key: str | None = None) -> None:
    api_key = key or _resolve_api_key(purpose)
    until = _load_blocked_until_ms(api_key)
    if until is None:
        return
    label = "Books" if purpose == "books" else "Chainlink"
    raise PmDataBlockedError(
        f"PMData {label} key temporarily blocked until {_format_until(until)} "
        f"(abnormal download activity). {label} Generate paused.",
        blocked_until_ms=until,
        purpose=purpose,
    )


def _raise_http_error(
    status: int,
    body: str,
    reason: str,
    *,
    purpose: PmDataPurpose,
    api_key: str,
) -> None:
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
            for token in msg.replace(",", " ").split():
                if "T" in token and token[0].isdigit():
                    blocked_until_ms = _parse_iso_ms(token.strip("."))
                    if blocked_until_ms is not None:
                        break
        if blocked_until_ms is not None:
            _store_blocked_until_ms(api_key, blocked_until_ms)
            label = "Books" if purpose == "books" else "Chainlink"
            raise PmDataBlockedError(
                f"PMData {label} key temporarily blocked until {_format_until(blocked_until_ms)}. "
                f"Stop {label} Generate until then.",
                blocked_until_ms=blocked_until_ms,
                purpose=purpose,
            )
    if status in (401, 403):
        raise RuntimeError(f"PMData denied ({status}): {detail}")
    raise RuntimeError(f"PMData download failed ({status}): {detail}")


def _download_bytes(
    url: str,
    *,
    purpose: PmDataPurpose,
    timeout_s: float = 180.0,
) -> bytes:
    api_key = _resolve_api_key(purpose)
    assert_pmdata_not_blocked(purpose, key=api_key)
    headers = {
        "api_key": api_key,
        "User-Agent": "poly-monitor/pmdata",
        "Accept": "application/octet-stream,*/*",
    }
    with _DOWNLOAD_SEM:
        assert_pmdata_not_blocked(purpose, key=api_key)
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 404:
                raise FileNotFoundError(f"PMData file not found: {url}")
            if resp.status_code >= 400:
                _raise_http_error(
                    resp.status_code,
                    resp.text or "",
                    resp.reason_phrase or "",
                    purpose=purpose,
                    api_key=api_key,
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
    Auth: PMDATA_API_KEY_BOOKS (fallback: PMDATA_API_KEY).
    """
    slug = str(slug or "").strip()
    if not slug:
        raise ValueError("slug required")

    cache = _cache_path(slug)
    if cache.is_file() and not force:
        return pd.read_parquet(cache)

    url = f"{PMDATA_BASE}/download/poly_l2/{slug}.parquet"
    raw = _download_bytes(url, purpose="books", timeout_s=timeout_s)

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
    Auth: PMDATA_API_KEY_CHAINLINK (fallback: PMDATA_API_KEY).
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
    raw = _download_bytes(url, purpose="chainlink", timeout_s=timeout_s)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".parquet.tmp")
    tmp.write_bytes(raw)
    tmp.replace(cache)
    return pd.read_parquet(cache)


def download_meta(slug: str) -> dict[str, Any]:
    """Lightweight probe: ensure Books API key works and file exists (uses cache)."""
    df = download_poly_l2(slug, force=False)
    return {"slug": slug, "n_rows": int(len(df)), "columns": list(df.columns)}
