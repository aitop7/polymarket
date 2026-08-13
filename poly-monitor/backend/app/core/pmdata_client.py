"""Download Polymarket L2 books from PMData (https://pmdata.dev)."""

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
    key = (settings.pmdata_api_key or "").strip()
    if not key:
        raise RuntimeError("PMDATA_API_KEY is not configured")

    slug = str(slug or "").strip()
    if not slug:
        raise ValueError("slug required")

    cache = _cache_path(slug)
    if cache.is_file() and not force:
        return pd.read_parquet(cache)

    url = f"{PMDATA_BASE}/download/poly_l2/{slug}.parquet"
    headers = {
        "api_key": key,
        "User-Agent": "poly-monitor/pm-orderbooks",
        "Accept": "application/octet-stream,*/*",
    }
    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code == 401 or resp.status_code == 403:
            raise RuntimeError(f"PMData auth failed ({resp.status_code}) — check PMDATA_API_KEY")
        if resp.status_code == 404:
            raise FileNotFoundError(f"PMData poly_l2 not found for slug={slug}")
        if resp.status_code >= 400:
            detail = (resp.text or "")[:240]
            raise RuntimeError(f"PMData download failed ({resp.status_code}): {detail}")
        raw = resp.content

    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".parquet.tmp")
    tmp.write_bytes(raw)
    tmp.replace(cache)
    return pd.read_parquet(cache)


def download_meta(slug: str) -> dict[str, Any]:
    """Lightweight probe: ensure API key works and file exists (uses cache)."""
    df = download_poly_l2(slug, force=False)
    return {"slug": slug, "n_rows": int(len(df)), "columns": list(df.columns)}
