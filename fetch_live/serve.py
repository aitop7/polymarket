"""Entry point: read-only HTTP API over fetch_live data (VPS).

Usage:
  cd fetch_live
  python serve.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn
from loguru import logger

from app.config import settings


def main() -> None:
    host = settings.serve_host
    port = int(settings.serve_port)
    logger.info(
        "Serving fetch_live data_dir={} on {}:{} (auth={})",
        settings.data_dir,
        host,
        port,
        "on" if (settings.api_token or "").strip() else "off",
    )
    uvicorn.run(
        "app.serve_api:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
