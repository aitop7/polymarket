from __future__ import annotations

import asyncio

from app.api.binance import BinanceClient
from app.api.polymarket import PolymarketClient
from app.config import settings
from app.storage.market_sessions import sessions
from app.storage.markets import markets
from app.utils.logger import logger
from app.utils.time import utcnow


class MetadataCollector:
    """Refresh market metadata into the per-market session buffer."""

    def __init__(
        self,
        poly: PolymarketClient | None = None,
        binance: BinanceClient | None = None,
    ) -> None:
        self.poly = poly or PolymarketClient()
        self.binance = binance or BinanceClient()
        self._owns = poly is None and binance is None
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.refresh_once()
            except Exception as exc:
                logger.exception("Metadata refresh error: {}", exc)
            await asyncio.sleep(settings.metadata_refresh_interval_s)

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        self.stop()
        if self._owns:
            await self.poly.close()
            await self.binance.close()

    async def refresh_once(self) -> int:
        markets.reload()
        active = markets.list_active()
        updated = 0

        for market in active:
            raw = await self.poly.get_market_by_slug(market.slug)
            if not raw:
                continue
            row = self.poly.normalize_market(raw)

            if market.start_time:
                try:
                    klines = await self.binance.get_klines(
                        interval="1m",
                        start_time=market.start_time,
                        limit=1,
                    )
                    if klines:
                        row["opening_btc_price"] = float(klines[0][1])
                except Exception as exc:
                    logger.warning("BTC open price fetch failed for {}: {}", market.market_id, exc)

            if market.end_time and market.end_time <= utcnow():
                try:
                    klines = await self.binance.get_klines(
                        interval="1m",
                        start_time=market.end_time,
                        limit=1,
                    )
                    if klines:
                        row["closing_btc_price"] = float(klines[0][4])
                except Exception as exc:
                    logger.warning("BTC close price fetch failed for {}: {}", market.market_id, exc)

            markets.upsert_one(row)
            sessions.set_market_meta(row)
            sessions.append(
                market.market_id,
                "meta",
                {**row, "timestamp": utcnow(), "slug": market.slug},
            )
            updated += 1

        logger.info("Metadata refreshed {} markets", updated)
        return updated
