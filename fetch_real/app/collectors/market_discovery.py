from __future__ import annotations

import asyncio

from app.api.pmxt_client import PmxtClient
from app.api.polymarket import PolymarketClient
from app.config import settings
from app.storage.market_sessions import sessions
from app.storage.markets import markets
from app.utils.logger import logger


class MarketDiscovery:
    """Discover 5m markets and open/finalize per-market parquet sessions."""

    def __init__(
        self,
        client: PolymarketClient | None = None,
        pmxt: PmxtClient | None = None,
    ) -> None:
        self.client = client or PolymarketClient()
        self.pmxt = pmxt or PmxtClient()
        self._owns_client = client is None
        self._owns_pmxt = pmxt is None
        self._running = False
        self._known: set[str] = set()

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.poll_once()
            except Exception as exc:
                logger.exception("Market discovery error: {}", exc)
            await asyncio.sleep(settings.market_discovery_interval_s)

    def stop(self) -> None:
        self._running = False
        sessions.flush_all_active()

    async def close(self) -> None:
        self.stop()
        if self._owns_client:
            await self.client.close()
        if self._owns_pmxt:
            await self.pmxt.close()

    async def poll_once(self) -> int:
        rows: list[dict] = []

        if settings.pmxt_enabled:
            try:
                rows = await self.pmxt.discover_btc_markets()
                logger.info("PMXT discovery returned {} markets", len(rows))
            except Exception as exc:
                logger.warning("PMXT discovery failed, falling back to Gamma: {}", exc)

        if not rows:
            discovered = await self.client.discover_btc_markets()
            try:
                closed = await self.client.list_markets(
                    slug_contains=settings.market_slug_prefix,
                    active=None,
                    closed=True,
                    limit=50,
                )
                discovered = discovered + closed
            except Exception as exc:
                logger.warning("Closed market poll failed: {}", exc)
            rows = [self.client.normalize_market(m) for m in discovered]

        by_id = {r["market_id"]: r for r in rows}
        rows = list(by_id.values())

        new_ids = {r["market_id"] for r in rows} - self._known
        if new_ids:
            logger.info("Discovered {} new markets", len(new_ids))
        self._known |= {r["market_id"] for r in rows}

        n = markets.upsert_many(rows)
        # checkpoint in-progress market files
        sessions.flush_all_active()
        logger.info("Market discovery updated {} markets (1 parquet file each)", n)
        return n
