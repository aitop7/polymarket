from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from app.config import settings
from app.storage.market_sessions import sessions
from app.storage.markets import markets
from app.utils.logger import logger
from app.utils.time import utcnow


class BtcCollector:
    """Stream Binance BTC trades into active market `btc.parquet` (1s last price)."""

    def __init__(self) -> None:
        self._running = False
        self._pending_sec: int | None = None
        self._pending_price: float | None = None

    async def run(self) -> None:
        self._running = True
        stream = f"{settings.btc_symbol.lower()}@trade"
        base = settings.binance_ws_url.rstrip("/").replace("/ws", "")
        url = f"{base}/stream?streams={stream}"

        while self._running:
            try:
                logger.info("BTC collector connecting {}", url)
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    async for raw in ws:
                        if not self._running:
                            break
                        self._handle_message(raw)
            except ConnectionClosed as exc:
                logger.warning("BTC websocket disconnected: {}", exc)
            except Exception as exc:
                logger.exception("BTC collector error: {}", exc)
            if self._running:
                await asyncio.sleep(2)

    def stop(self) -> None:
        self._running = False
        self._flush_pending()

    def _flush_pending(self) -> None:
        if self._pending_sec is None or self._pending_price is None:
            return
        self._emit(self._pending_sec, self._pending_price)
        self._pending_sec = None
        self._pending_price = None

    def _emit(self, ts_ms: int, price: float) -> None:
        active = markets.list_active()
        if not active:
            logger.debug("BTC tick ignored (no active markets) @ {}", utcnow())
            return
        sessions.append_btc_tick_to_active(
            {"timestamp": ts_ms, "price": price},
            active,
        )

    def _handle_message(self, raw: str | bytes) -> None:
        payload = json.loads(raw)
        data = payload.get("data", payload)
        if data.get("e") != "trade":
            return

        ts_ms = (int(data["T"]) // 1000) * 1000
        price = float(data["p"])

        if self._pending_sec is None:
            self._pending_sec = ts_ms
            self._pending_price = price
            return

        if ts_ms == self._pending_sec:
            self._pending_price = price
            return

        self._emit(self._pending_sec, float(self._pending_price))
        self._pending_sec = ts_ms
        self._pending_price = price
