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
from app.utils.time import ms_to_datetime, utcnow


class BtcCollector:
    """Stream Binance BTC ticks into active market `btc_ticks` tables."""

    def __init__(self) -> None:
        self._best_bid: float | None = None
        self._best_ask: float | None = None
        self._running = False

    async def run(self) -> None:
        self._running = True
        streams = f"{settings.btc_symbol.lower()}@trade/{settings.btc_symbol.lower()}@bookTicker"
        base = settings.binance_ws_url.rstrip("/").replace("/ws", "")
        url = f"{base}/stream?streams={streams}"

        while self._running:
            try:
                logger.info("BTC tick collector connecting {}", url)
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

    def _handle_message(self, raw: str | bytes) -> None:
        payload = json.loads(raw)
        data = payload.get("data", payload)
        event = data.get("e")

        if event == "bookTicker" or ("b" in data and "a" in data and "e" not in data):
            self._best_bid = float(data["b"])
            self._best_ask = float(data["a"])
            return

        if event != "trade":
            return

        tick = {
            "timestamp": ms_to_datetime(data["T"]),
            "price": float(data["p"]),
            "size": float(data["q"]),
            "side": "sell" if data.get("m") else "buy",
            "best_bid": self._best_bid,
            "best_ask": self._best_ask,
        }
        active = markets.list_active()
        if active:
            sessions.append_btc_tick_to_active(tick, active)
        else:
            logger.debug("BTC tick ignored (no active markets) @ {}", utcnow())
