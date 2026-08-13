"""Download Polymarket L2 books + Chainlink feeds from PMData (https://pmdata.dev)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from app.core.config import POLY_MONITOR_ROOT, settings

PMDATA_BASE = "https://api.pmdata.dev"
_CACHE_DIR = POLY_MONITOR_ROOT / "backend" / ".cache" / "pmdata"


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


def _download_bytes(url: str, *, timeout_s: float = 180.0) -> bytes:
    headers = {
        "api_key": _api_key(),
        "User-Agent": "poly-monitor/pmdata",
        "Accept": "application/octet-stream,*/*",
    }
    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code == 401 or resp.status_code == 403:
            raise RuntimeError(f"PMData auth failed ({resp.status_code}) — check PMDATA_API_KEY")
        if resp.status_code == 404:
            raise FileNotFoundError(f"PMData file not found: {url}")
        if resp.status_code >= 400:
            detail = (resp.text or "")[:240]
            raise RuntimeError(f"PMData download failed ({resp.status_code}): {detail}")
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
