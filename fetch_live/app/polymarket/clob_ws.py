"""Polymarket CLOB market WebSocket: trades + book updates."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import websockets
from loguru import logger
from websockets.exceptions import ConnectionClosed

from app.config import settings

# Callbacks
OnPmTrade = Callable[[dict[str, Any]], None]
OnBookUpdate = Callable[[str, dict[str, list[dict[str, float]]]], None]  # token_id, book


class InMemoryBook:
    """Simple price→size maps for one token."""

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def apply_snapshot(self, bids: list[dict[str, float]], asks: list[dict[str, float]]) -> None:
        self.bids = {round(float(x["price"]), 4): float(x["size"]) for x in bids if float(x["size"]) > 0}
        self.asks = {round(float(x["price"]), 4): float(x["size"]) for x in asks if float(x["size"]) > 0}

    def apply_level(self, side: str, price: float, size: float) -> None:
        book = self.bids if side == "buy" else self.asks
        p = round(float(price), 4)
        if size <= 0:
            book.pop(p, None)
        else:
            book[p] = float(size)

    def as_levels(self) -> dict[str, list[dict[str, float]]]:
        bids = [{"price": p, "size": s} for p, s in sorted(self.bids.items(), reverse=True)]
        asks = [{"price": p, "size": s} for p, s in sorted(self.asks.items())]
        return {"bids": bids, "asks": asks}


class ClobMarketWs:
    def __init__(
        self,
        *,
        on_trade: OnPmTrade | None = None,
        on_book: OnBookUpdate | None = None,
    ) -> None:
        self.on_trade = on_trade
        self.on_book = on_book
        self._running = False
        self._asset_token: dict[str, bool] = {}  # asset_id -> is_down (plan: 0=UP,1=DOWN)
        self._asset_market: dict[str, str] = {}
        self._books: dict[str, InMemoryBook] = {}
        self._want_assets: list[str] = []
        self._last_up_price: float | None = None
        self._last_down_price: float | None = None

    @property
    def last_up_price(self) -> float | None:
        return self._last_up_price

    @property
    def last_down_price(self) -> float | None:
        return self._last_down_price

    def set_subscriptions(
        self,
        *,
        market_id: str,
        token_up: str | None,
        token_down: str | None,
    ) -> None:
        mapping: dict[str, bool] = {}
        assets: list[str] = []
        self._asset_market.clear()
        if token_up:
            mapping[token_up] = False  # UP => token False (0)
            assets.append(token_up)
            self._asset_market[token_up] = market_id
            self._books.setdefault(token_up, InMemoryBook())
        if token_down:
            mapping[token_down] = True  # DOWN => token True (1)
            assets.append(token_down)
            self._asset_market[token_down] = market_id
            self._books.setdefault(token_down, InMemoryBook())
        self._asset_token = mapping
        self._want_assets = assets

    def seed_book(self, token_id: str, levels: dict[str, list[dict[str, float]]]) -> None:
        book = self._books.setdefault(token_id, InMemoryBook())
        book.apply_snapshot(levels.get("bids") or [], levels.get("asks") or [])

    def get_book_levels(self, token_id: str | None) -> dict[str, list[dict[str, float]]]:
        if not token_id or token_id not in self._books:
            return {"bids": [], "asks": []}
        return self._books[token_id].as_levels()

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                if not self._want_assets:
                    await asyncio.sleep(1.0)
                    continue
                await self._stream(list(self._want_assets))
            except Exception as exc:
                logger.exception("CLOB WS error: {}", exc)
                await asyncio.sleep(2)

    async def _stream(self, asset_ids: list[str]) -> None:
        url = settings.polymarket_market_ws
        logger.info("CLOB market WS connecting assets={}", len(asset_ids))
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
            ping_task = asyncio.create_task(self._ping(ws))
            subscribed = set(asset_ids)
            try:
                async for raw in ws:
                    if not self._running:
                        break
                    # Resubscribe if assets changed
                    if set(self._want_assets) != subscribed:
                        break
                    if raw == "PONG":
                        continue
                    self._handle(raw)
            except ConnectionClosed as exc:
                logger.warning("CLOB WS closed: {}", exc)
            finally:
                ping_task.cancel()

    async def _ping(self, ws: Any) -> None:
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
            et = str(event.get("event_type") or event.get("type") or "")
            if et == "last_trade_price":
                self._on_trade(event)
            elif et in {"book", "price_change"}:
                self._on_book_event(event, et)

    def _on_trade(self, event: dict[str, Any]) -> None:
        asset_id = str(event.get("asset_id") or "")
        is_down = self._asset_token.get(asset_id)
        if is_down is None:
            return
        try:
            ts = int(event.get("timestamp") or 0)
            if ts < 10_000_000_000:
                ts *= 1000
            price = float(event.get("price") or 0)
            size = float(event.get("size") or 0)
        except (TypeError, ValueError):
            return
        side_raw = str(event.get("side") or "BUY").upper()
        # plan: side 0=BUY, 1=SELL
        side = side_raw in {"SELL", "S"}
        if is_down:
            self._last_down_price = price
        else:
            self._last_up_price = price
        if not self.on_trade:
            return
        row = {
            "timestamp": ts,
            "wallet": str(event.get("maker_address") or event.get("taker_address") or ""),
            "token": bool(is_down),
            "side": bool(side),
            "price": price,
            "shares": max(0, min(int(round(size)), 2**32 - 1)),
        }
        self.on_trade(row)

    def _on_book_event(self, event: dict[str, Any], et: str) -> None:
        asset_id = str(event.get("asset_id") or "")
        if asset_id not in self._books:
            # sometimes market-level events
            changes = event.get("price_changes") or event.get("changes") or []
            if isinstance(changes, list):
                for ch in changes:
                    if not isinstance(ch, dict):
                        continue
                    aid = str(ch.get("asset_id") or asset_id)
                    if aid not in self._books:
                        continue
                    self._apply_change(aid, ch)
            return
        book = self._books[asset_id]
        if et == "book":
            bids = [
                {"price": float(x["price"]), "size": float(x["size"])}
                for x in (event.get("bids") or [])
                if x.get("price") is not None
            ]
            asks = [
                {"price": float(x["price"]), "size": float(x["size"])}
                for x in (event.get("asks") or [])
                if x.get("price") is not None
            ]
            book.apply_snapshot(bids, asks)
        else:
            self._apply_change(asset_id, event)
            for ch in event.get("price_changes") or []:
                if isinstance(ch, dict):
                    aid = str(ch.get("asset_id") or asset_id)
                    if aid in self._books:
                        self._apply_change(aid, ch)
        if self.on_book:
            self.on_book(asset_id, book.as_levels())

    def _apply_change(self, asset_id: str, ch: dict[str, Any]) -> None:
        book = self._books.get(asset_id)
        if book is None:
            return
        side = str(ch.get("side") or "").lower()
        try:
            price = float(ch.get("price"))
            size = float(ch.get("size") or ch.get("quantity") or 0)
        except (TypeError, ValueError):
            return
        if side in {"buy", "bid"}:
            book.apply_level("buy", price, size)
        elif side in {"sell", "ask"}:
            book.apply_level("sell", price, size)
