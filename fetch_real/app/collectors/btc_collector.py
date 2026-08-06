from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from app.config import settings
from app.storage.market_sessions import sessions
from app.storage.markets import markets
from app.utils.logger import logger
from app.utils.time import ms_to_datetime, utcnow


class BtcCollector:
    """
    Aggregate Binance BTC into 1-second bars and attach them to active
    5-minute market session files (no separate tick / global BTC files).
    """

    def __init__(self) -> None:
        self._best_bid: float | None = None
        self._best_ask: float | None = None
        self._bucket_sec: int | None = None
        self._bar: dict[str, Any] | None = None
        self._running = False

    async def run(self) -> None:
        self._running = True
        streams = f"{settings.btc_symbol.lower()}@trade/{settings.btc_symbol.lower()}@bookTicker"
        base = settings.binance_ws_url.rstrip("/").replace("/ws", "")
        url = f"{base}/stream?streams={streams}"

        while self._running:
            try:
                logger.info("BTC 1s collector connecting {}", url)
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    flusher = asyncio.create_task(self._tick_loop())
                    try:
                        async for raw in ws:
                            if not self._running:
                                break
                            self._handle_message(raw)
                    finally:
                        flusher.cancel()
                        self._close_bucket()
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

        ts = ms_to_datetime(data["T"])
        price = float(data["p"])
        size = float(data["q"])
        side = "sell" if data.get("m") else "buy"
        sec = int(ts.timestamp())

        if self._bucket_sec is None:
            self._start_bar(sec, ts, price)
        elif sec > self._bucket_sec:
            self._close_bucket()
            self._start_bar(sec, ts, price)
        elif sec < self._bucket_sec:
            return

        assert self._bar is not None
        self._bar["high"] = max(self._bar["high"], price)
        self._bar["low"] = min(self._bar["low"], price)
        self._bar["close"] = price
        self._bar["volume"] += size
        self._bar["trade_count"] += 1
        if side == "buy":
            self._bar["buy_volume"] += size
        else:
            self._bar["sell_volume"] += size
        self._bar["best_bid"] = self._best_bid
        self._bar["best_ask"] = self._best_ask

    def _start_bar(self, sec: int, ts: datetime, price: float) -> None:
        self._bucket_sec = sec
        self._bar = {
            "timestamp": ts.replace(microsecond=0),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 0.0,
            "trade_count": 0,
            "buy_volume": 0.0,
            "sell_volume": 0.0,
            "best_bid": self._best_bid,
            "best_ask": self._best_ask,
        }

    def _close_bucket(self) -> None:
        if self._bar is None:
            return
        bar = self._bar
        self._bar = None
        self._bucket_sec = None
        active = markets.list_active()
        if active:
            sessions.append_btc_to_active(bar, active)
        logger.debug("BTC 1s bar close={} vol={} markets={}", bar["close"], bar["volume"], len(active))

    async def _tick_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.25)
            now_sec = int(utcnow().timestamp())
            if self._bucket_sec is not None and now_sec > self._bucket_sec:
                self._close_bucket()
