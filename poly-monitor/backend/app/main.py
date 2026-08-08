"""FastAPI entrypoint for poly-monitor."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Ensure backend/ and poly-monitor/ (for strategies/) are importable
_BACKEND = Path(__file__).resolve().parents[1]
_POLY = Path(__file__).resolve().parents[2]
_FRONTEND_DIST = _POLY / "frontend" / "dist"
for p in (_BACKEND, _POLY):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from app.api.routes import router  # noqa: E402
from app.core.config import settings  # noqa: E402

app = FastAPI(title="poly-monitor", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix="/api")

_ASSETS = _FRONTEND_DIST / "assets"
if _ASSETS.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_ASSETS)), name="assets")


def _spa_index() -> FileResponse:
    index = _FRONTEND_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(
            status_code=503,
            detail="Frontend not built. Run: cd poly-monitor/frontend && npm run build",
        )
    return FileResponse(index)


@app.get("/")
def root():
    if (_FRONTEND_DIST / "index.html").is_file():
        return _spa_index()
    return {"service": "poly-monitor", "docs": "/docs"}


@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    """Serve built SPA assets and client routes (when frontend/dist exists)."""
    if full_path.startswith(("api/", "api", "docs", "openapi", "redoc")):
        raise HTTPException(status_code=404, detail="Not found")
    candidate = _FRONTEND_DIST / full_path
    if candidate.is_file():
        return FileResponse(candidate)
    if (_FRONTEND_DIST / "index.html").is_file():
        return _spa_index()
    raise HTTPException(
        status_code=503,
        detail="Frontend not built. Run: cd poly-monitor/frontend && npm run build",
    )
