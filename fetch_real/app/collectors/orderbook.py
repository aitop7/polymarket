from __future__ import annotations

import asyncio
from typing import Any

from app.api.pmxt_client import PmxtClient
from app.api.polymarket import PolymarketClient
from app.config import settings
from app.features import FeatureEngine
from app.features.depth_bands import build_orderbook_row
from app.features.spread import top_levels
from app.storage.market_sessions import sessions
from app.storage.markets import markets
from app.utils.logger import logger
from app.utils.time import utcnow


class OrderBookCollector:
    """Poll Up+Down books; write flat storage-optimized orderbook rows."""

    def __init__(
        self,
        client: PolymarketClient | None = None,
        pmxt: PmxtClient | None = None,
        features: FeatureEngine | None = None,
    ) -> None:
        self.client = client or PolymarketClient()
        self.pmxt = pmxt or PmxtClient()
        self.features = features or FeatureEngine()
        self._owns_client = client is None
        self._owns_pmxt = pmxt is None
        self._running = False
        self._flush_every = 30
        self._sem = asyncio.Semaphore(max(1, settings.download_concurrency))
        self._last_trade_px: dict[str, dict[str, float]] = {}

    async def run(self) -> None:
        self._running = True
        interval = settings.orderbook_interval_ms / 1000.0
        ticks = 0
        while self._running:
            started = asyncio.get_event_loop().time()
            try:
                await self.snapshot_once()
                ticks += 1
                if ticks % self._flush_every == 0:
                    sessions.flush_all_active()
                    self._finalize_expired()
            except Exception as exc:
                logger.exception("Orderbook collector error: {}", exc)
            elapsed = asyncio.get_event_loop().time() - started
            await asyncio.sleep(max(0.0, interval - elapsed))

    def stop(self) -> None:
        self._running = False
        sessions.flush_all_active()

    async def close(self) -> None:
        self.stop()
        if self._owns_client:
            await self.client.close()
        if self._owns_pmxt:
            await self.pmxt.close()

    def note_trade_price(self, market_id: str, outcome: str, price: float) -> None:
        key = "up" if str(outcome).lower() in {"yes", "up"} else "down"
        self._last_trade_px.setdefault(market_id, {})[key] = float(price)

    def _finalize_expired(self) -> None:
        expired = markets.list_expired_active()
        for m in expired:
            m.status = "closed"
            sessions.set_market_meta(m.as_dict())
            sessions.finalize(m.market_id)
            markets.upsert_one(m.as_dict())

    async def _fetch_book(self, token_id: str) -> dict[str, Any]:
        async with self._sem:
            if settings.pmxt_enabled:
                try:
                    book = await self.pmxt.fetch_order_book(token_id)
                    if isinstance(book, list):
                        return book[-1] if book else {"bids": [], "asks": []}
                    return book
                except Exception as exc:
                    logger.debug("PMXT orderbook miss for {}: {}", token_id[:16], exc)
            return await self.client.get_order_book(token_id)

    @staticmethod
    def _levels(book: dict[str, Any] | None, side: str) -> list[dict[str, float]]:
        if not book:
            return []
        return top_levels(book, side, 50)

    async def _snapshot_market(self, market: Any, ts: Any) -> bool:
        token_yes = market.token_yes
        token_no = market.token_no
        if not token_yes and not token_no:
            return False

        up_book = down_book = None
        try:
            if token_yes:
                up_book = await self._fetch_book(token_yes)
            if token_no:
                try:
                    down_book = await self._fetch_book(token_no)
                except Exception as exc:
                    logger.debug("Down book miss {}: {}", market.market_id, exc)
        except Exception as exc:
            logger.warning("Missed orderbook snapshot for {}: {}", market.market_id, exc)
            return False

        if not up_book and not down_book:
            return False

        px = self._last_trade_px.get(market.market_id) or {}
        row = build_orderbook_row(
            timestamp=ts,
            up_bids=self._levels(up_book, "bids"),
            up_asks=self._levels(up_book, "asks"),
            down_bids=self._levels(down_book, "bids"),
            down_asks=self._levels(down_book, "asks"),
            up_price=px.get("up"),
            down_price=px.get("down"),
        )
        primary = up_book or down_book or {"bids": [], "asks": []}
        feat = self.features.compute(
            market_id=market.market_id,
            book=primary,
            settlement_time=market.settlement_time or market.end_time,
            timestamp=ts,
        )
        sessions.set_market_meta(market.as_dict())
        sessions.append(market.market_id, "orderbook", row)
        sessions.append(
            market.market_id,
            "feature",
            {
                "timestamp": feat["timestamp"],
                "spread": feat["spread"],
                "imbalance": feat["imbalance"],
                "momentum": feat["momentum"],
                "volatility": feat["volatility"],
                "depth": feat["depth"],
                "whale_score": feat["whale_score"],
                "time_remaining": feat["time_remaining"],
            },
        )
        return True

    async def snapshot_once(self) -> int:
        markets.reload()
        active = markets.list_active()
        ts = utcnow()
        results = await asyncio.gather(
            *[self._snapshot_market(m, ts) for m in active],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)
