"""Combined Binance WS: aggTrade + bookTicker + depth."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

import httpx
import websockets
from loguru import logger
from websockets.exceptions import ConnectionClosed

from app.binance.local_book import LocalOrderBook
from app.config import settings

OnTrade = Callable[[int, int, float, float, bool], None]  # agg_id, ts, price, qty, maker
OnBookTicker = Callable[[int, float, float, float, float], None]
OnDepthReady = Callable[[], None]


class BinanceHub:
    def __init__(
        self,
        *,
        on_trade: OnTrade | None = None,
        on_bookticker: OnBookTicker | None = None,
    ) -> None:
        self.on_trade = on_trade
        self.on_bookticker = on_bookticker
        self.book = LocalOrderBook()
        self._running = False
        self._diff_buffer: list[dict[str, Any]] = []
        self._syncing = False
        self._needs_resync = False
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))

    async def close(self) -> None:
        self._running = False
        await self._http.aclose()

    async def run(self) -> None:
        self._running = True
        symbol = settings.btc_symbol.lower()
        streams = f"{symbol}@aggTrade/{symbol}@bookTicker/{symbol}@depth@100ms"
        bases = []
        for b in (settings.binance_ws_url, *settings.binance_ws_fallbacks):
            b = b.rstrip("/").replace("/ws", "")
            if b not in bases:
                bases.append(b)
        idx = 0
        while self._running:
            base = bases[idx % len(bases)]
            url = f"{base}/stream?streams={streams}"
            try:
                logger.info("Binance hub connecting {}", url)
                self.book.reset()
                self._diff_buffer.clear()
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    sync_task = asyncio.create_task(self._sync_loop())
                    try:
                        async for raw in ws:
                            if not self._running:
                                break
                            self._handle(raw)
                    finally:
                        sync_task.cancel()
            except ConnectionClosed as exc:
                logger.warning("Binance WS closed: {}", exc)
            except Exception as exc:
                logger.warning("Binance hub error: {} — trying next endpoint", exc)
                idx += 1
            if self._running:
                await asyncio.sleep(2)

    async def _sync_loop(self) -> None:
        await asyncio.sleep(0.3)
        while self._running:
            if not self.book.ready or self._needs_resync:
                try:
                    await self._resync()
                    self._needs_resync = False
                except Exception as exc:
                    logger.warning("Binance depth sync failed: {}", exc)
                    await asyncio.sleep(1.0)
                    continue
            await asyncio.sleep(0.5)

    async def _ensure_synced(self) -> None:
        await self._sync_loop()

    async def _resync(self) -> None:
        self._syncing = True
        symbol = settings.btc_symbol.upper()
        url = f"{settings.binance_rest_url.rstrip('/')}/api/v3/depth"
        resp = await self._http.get(url, params={"symbol": symbol, "limit": 1000})
        resp.raise_for_status()
        snap = resp.json()
        last_id = int(snap.get("lastUpdateId") or 0)
        # Drop buffered diffs older than snapshot
        kept = [e for e in self._diff_buffer if int(e.get("u") or 0) > last_id]
        self.book.apply_snapshot(snap)
        for ev in kept:
            if not self.book.apply_diff(ev):
                self.book.reset()
                self._diff_buffer = kept
                self._syncing = False
                raise RuntimeError("depth gap after snapshot")
        self._diff_buffer.clear()
        self._syncing = False
        logger.info("Binance local book synced lastUpdateId={}", last_id)

    def _handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        data = msg.get("data") if isinstance(msg, dict) and "data" in msg else msg
        if not isinstance(data, dict):
            return
        event = data.get("e")
        if event == "aggTrade":
            self._on_agg_trade(data)
        elif event == "bookTicker":
            self._on_book_ticker(data)
        elif event == "depthUpdate":
            self._on_depth(data)
        elif (
            "B" in data
            and "A" in data
            and isinstance(data.get("b"), str)
            and isinstance(data.get("a"), str)
        ):
            # Combined-stream bookTicker often omits event type `e`.
            self._on_book_ticker(data)

    def _on_agg_trade(self, data: dict[str, Any]) -> None:
        if not self.on_trade:
            return
        try:
            agg_id = int(data["a"])
            ts = int(data["T"])
            price = float(data["p"])
            qty = float(data["q"])
            maker = bool(data["m"])
        except (KeyError, TypeError, ValueError):
            return
        self.on_trade(agg_id, ts, price, qty, maker)

    def _on_book_ticker(self, data: dict[str, Any]) -> None:
        if not self.on_bookticker:
            return
        try:
            ts = int(data.get("E") or time.time() * 1000)
            bid_p = float(data["b"])
            bid_q = float(data["B"])
            ask_p = float(data["a"])
            ask_q = float(data["A"])
        except (KeyError, TypeError, ValueError):
            return
        self.on_bookticker(ts, bid_p, bid_q, ask_p, ask_q)

    def _on_depth(self, data: dict[str, Any]) -> None:
        if not self.book.ready:
            self._diff_buffer.append(data)
            if len(self._diff_buffer) > 5000:
                self._diff_buffer = self._diff_buffer[-2000:]
            return
        ok = self.book.apply_diff(data)
        if not ok:
            logger.warning("Binance depth gap — resync")
            self.book.reset()
            self._diff_buffer = [data]
            self._needs_resync = True

    async def _resync_safe(self) -> None:
        try:
            await self._resync()
        except Exception as exc:
            logger.warning("resync failed: {}", exc)
