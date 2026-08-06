from __future__ import annotations

import asyncio
from typing import Any

from app.storage.market_sessions import sessions
from app.utils.logger import logger
from app.utils.time import utcnow


class OrderCollector:
    """Order events go into the related market's single parquet file when market_id is present."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                self._ingest(event)
            except TimeoutError:
                continue
            except Exception as exc:
                logger.exception("Order collector error: {}", exc)

    def stop(self) -> None:
        self._running = False

    async def ingest(self, event: dict[str, Any]) -> None:
        await self._queue.put(event)

    def _ingest(self, event: dict[str, Any]) -> None:
        market_id = str(event.get("market_id") or event.get("market") or "")
        order_id = str(event.get("order_id") or event.get("id") or "")
        if not market_id or not order_id:
            return
        sessions.append(
            market_id,
            "order",
            {
                "timestamp": event.get("timestamp") or utcnow(),
                "market_id": market_id,
                "order_id": order_id,
                "wallet": event.get("wallet") or event.get("owner"),
                "price": float(event["price"]) if event.get("price") is not None else None,
                "size": float(event["size"]) if event.get("size") is not None else None,
                "event_type": str(event.get("event_type") or event.get("type") or "NEW").upper(),
                "raw_json": event,
            },
        )
