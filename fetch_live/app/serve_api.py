"""Read-only HTTP API over fetch_live data_dir (VPS mirror for local sync)."""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from app.config import settings
from app.schemas import TABLE_FILES

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MIN_START_MS = 1_600_000_000_000  # ~2020-09 — skip epoch junk dirs

ALLOWED_FILES = frozenset({"meta.json", *TABLE_FILES.values()})


def _data_dir() -> Path:
    return Path(settings.data_dir).resolve()


def require_bearer(authorization: str | None = Header(default=None)) -> None:
    token = (settings.api_token or "").strip()
    if not token:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if authorization[len("Bearer ") :].strip() != token:
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def _read_meta(meta_path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _file_listing(market_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for name in sorted(ALLOWED_FILES):
        p = market_dir / name
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        files.append(
            {
                "name": name,
                "size": int(st.st_size),
                "mtime_ms": int(st.st_mtime * 1000),
            }
        )
    return files


def _catalog_entry(day: str, market_dir: Path, meta: dict[str, Any]) -> dict[str, Any] | None:
    try:
        start_ms = int(meta.get("start_time") or 0)
    except (TypeError, ValueError):
        return None
    if start_ms < _MIN_START_MS:
        return None
    try:
        end_ms = int(meta.get("end_time") or 0)
    except (TypeError, ValueError):
        end_ms = 0
    market_id = str(meta.get("market_id") or market_dir.name)
    files = _file_listing(market_dir)
    mtime_ms = max((f["mtime_ms"] for f in files), default=0)
    meta_path = market_dir / "meta.json"
    if meta_path.is_file():
        try:
            mtime_ms = max(mtime_ms, int(meta_path.stat().st_mtime * 1000))
        except OSError:
            pass
    return {
        "market_id": market_id,
        "date": day,
        "start_time": start_ms,
        "end_time": end_ms,
        "active": bool(meta.get("active")),
        "closed": bool(meta.get("closed")),
        "slug": str(meta.get("slug") or ""),
        "mtime_ms": mtime_ms,
        "files": files,
    }


def iter_catalog(*, after_start_ms: int | None = None) -> list[dict[str, Any]]:
    root = _data_dir()
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
            entry = _catalog_entry(day_dir.name, market_dir, meta)
            if entry is None:
                continue
            if after_start_ms is not None and entry["start_time"] <= int(after_start_ms):
                continue
            out.append(entry)
    out.sort(key=lambda e: (int(e["start_time"]), str(e["market_id"])))
    return out


def find_market_dir(market_id: str) -> tuple[str, Path] | None:
    mid = str(market_id).strip()
    if not mid:
        return None
    root = _data_dir()
    if not root.is_dir():
        return None
    # Prefer newest date dirs first
    days = sorted(
        (d for d in root.iterdir() if d.is_dir() and _DAY_RE.match(d.name)),
        reverse=True,
    )
    for day_dir in days:
        candidate = day_dir / mid
        if candidate.is_dir() and (candidate / "meta.json").is_file():
            return day_dir.name, candidate
    return None


def create_app() -> FastAPI:
    app = FastAPI(title="fetch_live-serve", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, Any]:
        root = _data_dir()
        return {
            "ok": True,
            "data_dir": str(root),
            "exists": root.is_dir(),
        }

    @app.get("/markets", dependencies=[Depends(require_bearer)])
    def list_markets(
        after_start_ms: int | None = Query(default=None),
    ) -> dict[str, Any]:
        markets = iter_catalog(after_start_ms=after_start_ms)
        return {"markets": markets, "count": len(markets)}

    @app.get("/markets/{market_id}", dependencies=[Depends(require_bearer)])
    def market_detail(market_id: str) -> dict[str, Any]:
        found = find_market_dir(market_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Market not found")
        day, market_dir = found
        meta = _read_meta(market_dir / "meta.json") or {}
        entry = _catalog_entry(day, market_dir, meta)
        if entry is None:
            raise HTTPException(status_code=404, detail="Market meta invalid")
        return entry

    @app.get(
        "/markets/{market_id}/files/{name}",
        dependencies=[Depends(require_bearer)],
    )
    def market_file(market_id: str, name: str) -> FileResponse:
        if name not in ALLOWED_FILES:
            raise HTTPException(status_code=400, detail="Invalid file name")
        found = find_market_dir(market_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Market not found")
        path = found[1] / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        media = "application/json" if name.endswith(".json") else "application/octet-stream"
        return FileResponse(path, media_type=media, filename=name)

    @app.get(
        "/markets/{market_id}/archive",
        dependencies=[Depends(require_bearer)],
    )
    def market_archive(market_id: str) -> StreamingResponse:
        found = find_market_dir(market_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Market not found")
        day, market_dir = found
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "date.txt",
                day + "\n",
            )
            for name in sorted(ALLOWED_FILES):
                p = market_dir / name
                if p.is_file():
                    zf.write(p, arcname=name)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{market_id}.zip"',
                "X-Market-Date": day,
            },
        )

    return app


app = create_app()
