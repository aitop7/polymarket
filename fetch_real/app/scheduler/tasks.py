from __future__ import annotations

import asyncio
from typing import Any

from app.collectors import (
    BtcCollector,
    MarketDiscovery,
    MetadataCollector,
    OrderBookCollector,
    OrderCollector,
    TradeCollector,
    WalletCollector,
)
from app.features import FeatureEngine
from app.utils.logger import logger


class CollectorScheduler:
    """Run all realtime collectors on a shared asyncio event loop."""

    def __init__(self) -> None:
        self.features = FeatureEngine()
        self.btc = BtcCollector()
        self.discovery = MarketDiscovery()
        self.metadata = MetadataCollector()
        self.orderbook = OrderBookCollector(features=self.features)
        self.trades = TradeCollector(
            features=self.features,
            on_trade_price=self.orderbook.note_trade_price,
        )
        self.wallet = WalletCollector()
        self.orders = OrderCollector()
        self._tasks: list[asyncio.Task[Any]] = []
        self._running = False

    async def start(self) -> None:
        self._running = True
        # Seed markets once before streaming collectors start
        try:
            await self.discovery.poll_once()
        except Exception as exc:
            logger.warning("Initial market discovery failed: {}", exc)

        collectors = [
            ("btc", self.btc.run()),
            ("discovery", self.discovery.run()),
            ("metadata", self.metadata.run()),
            ("orderbook", self.orderbook.run()),
            ("trades", self.trades.run()),
            ("wallet", self.wallet.run()),
            ("orders", self.orders.run()),
        ]
        for name, coro in collectors:
            task = asyncio.create_task(self._supervise(name, coro), name=name)
            self._tasks.append(task)
        logger.info("Scheduler started {} collectors", len(self._tasks))

    async def _supervise(self, name: str, coro: Any) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            logger.info("Collector {} cancelled", name)
            raise
        except Exception as exc:
            logger.exception("Collector {} crashed: {}", name, exc)

    async def stop(self) -> None:
        self._running = False
        self.btc.stop()
        self.discovery.stop()
        self.metadata.stop()
        self.orderbook.stop()
        self.trades.stop()
        self.wallet.stop()
        self.orders.stop()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.discovery.close()
        await self.metadata.close()
        await self.orderbook.close()
        logger.info("Scheduler stopped")

    async def run_forever(self) -> None:
        await self.start()
        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            await self.stop()
