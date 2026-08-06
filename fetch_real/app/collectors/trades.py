from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from app.config import settings
from app.features.trade_schema import build_trade_row
from app.storage.market_sessions import sessions
from app.storage.markets import markets
from app.utils.logger import logger
from app.utils.time import ms_to_datetime, utcnow


class TradeCollector:
    """Stream Polymarket fills into per-market `trades.parquet`."""

    def __init__(self, on_trade_price: Any | None = None) -> None:
        self.on_trade_price = on_trade_price
        self._running = False
        self._asset_to_market: dict[str, str] = {}
        self._asset_to_slug: dict[str, str] = {}
        self._asset_to_token: dict[str, bool] = {}

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
        tokens: dict[str, bool] = {}
        for m in markets.list_active():
            if m.token_yes:
                mapping[m.token_yes] = m.market_id
                slugs[m.token_yes] = m.slug
                tokens[m.token_yes] = True  # UP
            if m.token_no:
                mapping[m.token_no] = m.market_id
                slugs[m.token_no] = m.slug
                tokens[m.token_no] = False  # DOWN
        self._asset_to_market = mapping
        self._asset_to_slug = slugs
        self._asset_to_token = tokens

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
            token = self._asset_to_token.get(asset_id)
            if self.on_trade_price is not None and token is not None:
                self.on_trade_price(market_id, "up" if token else "down", price)
            row = build_trade_row(
                timestamp=ts,
                wallet=event.get("maker_address") or event.get("taker_address"),
                price=price,
                size=size,
                side=side,
                token=token,
            )
            if row is None:
                continue
            sessions.append(market_id, "trade", row)
