"""FastAPI entrypoint for poly-monitor."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
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
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Background: refresh finished TWAP history from VPS every minute (never live window).
    async def _vps_sync_loop() -> None:
        from app.live.vps_sync import PERIODIC_SYNC_S, get_vps_sync

        sync = get_vps_sync()
        if not sync.enabled:
            logger.info(
                "VPS sync disabled (set VPS_SYNC_URL); local live dir=%s",
                settings.fetch_live_data_dir,
            )
            return
        while True:
            try:
                result = await sync.sync_tick()
                if result.get("error"):
                    logger.warning(
                        "VPS sync tick: %s (local dir=%s)",
                        result.get("error"),
                        settings.fetch_live_data_dir,
                    )
                else:
                    pulled = int(result.get("pulled") or 0)
                    repaired = int(result.get("repaired") or 0)
                    if pulled or repaired:
                        logger.info(
                            "VPS history sync: pulled=%s repaired=%s → %s",
                            pulled,
                            repaired,
                            settings.fetch_live_data_dir,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("VPS sync loop error: %s", exc)
            try:
                await asyncio.sleep(PERIODIC_SYNC_S)
            except asyncio.CancelledError:
                raise

    sync_task = asyncio.create_task(_vps_sync_loop(), name="vps-periodic-sync")
    yield
    sync_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="poly-monitor", version="0.1.0", lifespan=lifespan)
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
