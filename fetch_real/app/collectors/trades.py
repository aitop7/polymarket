from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from app.config import settings
from app.features import FeatureEngine
from app.storage.market_sessions import sessions
from app.storage.markets import markets
from app.utils.logger import logger
from app.utils.time import ms_to_datetime, utcnow


class TradeCollector:
    """Stream trades into the per-market session buffer (one parquet per 5m market)."""

    def __init__(self, features: FeatureEngine | None = None) -> None:
        self.features = features or FeatureEngine()
        self._running = False
        self._asset_to_market: dict[str, str] = {}
        self._asset_to_slug: dict[str, str] = {}

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                self._refresh_subscriptions()
                if not self._asset_to_market:
                    logger.info("Trade collector waiting for active markets...")
                    await asyncio.sleep(settings.market_discovery_interval_s)
                    continue
                await self._stream()
            except Exception as exc:
                logger.exception("Trade collector error: {}", exc)
                await asyncio.sleep(2)

    def stop(self) -> None:
        self._running = False

    def _refresh_subscriptions(self) -> None:
        markets.reload()
        mapping: dict[str, str] = {}
        slugs: dict[str, str] = {}
        for m in markets.list_active():
            if m.token_yes:
                mapping[m.token_yes] = m.market_id
                slugs[m.token_yes] = m.slug
            if m.token_no:
                mapping[m.token_no] = m.market_id
                slugs[m.token_no] = m.slug
        self._asset_to_market = mapping
        self._asset_to_slug = slugs

    async def _stream(self) -> None:
        url = settings.polymarket_market_ws
        asset_ids = list(self._asset_to_market.keys())
        logger.info("Trade collector connecting {} assets={}", url, len(asset_ids))
        async with websockets.connect(url, ping_interval=None) as ws:
            await ws.send(
                json.dumps(
                    {
                        "assets_ids": asset_ids,
                        "type": "market",
                        "custom_feature_enabled": True,
                    }
                )
            )
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    if not self._running:
                        break
                    if raw == "PONG":
                        continue
                    self._handle(raw)
            except ConnectionClosed as exc:
                logger.warning("Polymarket trade WS disconnected: {}", exc)
            finally:
                ping_task.cancel()

    async def _ping_loop(self, ws: Any) -> None:
        while self._running:
            await asyncio.sleep(10)
            try:
                await ws.send("PING")
            except Exception:
                return

    def _handle(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return

        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("event_type") != "last_trade_price":
                continue
            asset_id = str(event.get("asset_id") or "")
            market_id = self._asset_to_market.get(asset_id) or str(event.get("market") or "")
            if not market_id:
                continue
            ts_raw = event.get("timestamp")
            ts = ms_to_datetime(ts_raw) if ts_raw is not None else utcnow()
            price = float(event.get("price") or 0)
            size = float(event.get("size") or 0)
            side = str(event.get("side") or "buy").lower()
            trade_id = str(
                event.get("transaction_hash")
                or event.get("hash")
                or f"{asset_id}-{ts_raw}-{price}-{size}"
            )
            self.features.note_trade(market_id, size, price)
            sessions.append(
                market_id,
                "trade",
                {
                    "timestamp": ts,
                    "market_id": market_id,
                    "slug": self._asset_to_slug.get(asset_id),
                    "trade_id": trade_id,
                    "price": price,
                    "size": size,
                    "side": side,
                    "wallet": event.get("maker_address") or event.get("taker_address"),
                    "asset_id": asset_id,
                },
            )
