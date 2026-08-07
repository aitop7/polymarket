"""FastAPI entrypoint for poly-monitor."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Ensure backend/ and poly-monitor/ (for strategies/) are importable
_BACKEND = Path(__file__).resolve().parents[1]
_POLY = Path(__file__).resolve().parents[2]
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


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "poly-monitor", "docs": "/docs"}
