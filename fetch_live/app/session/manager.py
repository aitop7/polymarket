"""Active market session lifecycle."""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from loguru import logger

from app.discovery import Discovery, parse_token_ids, parse_window
from app.storage.market_store import MarketStore
from app.twap_open import TwapOpenResolver

OnMarketChange = Callable[..., Awaitable[None]]
OnMarketEnd = Callable[[MarketStore], Awaitable[None]]


class SessionManager:
    def __init__(
        self,
        discovery: Discovery,
        twap: TwapOpenResolver,
        *,
        on_market_change: OnMarketChange | None = None,
        on_market_end: OnMarketEnd | None = None,
    ) -> None:
        self.discovery = discovery
        self.twap = twap
        self.on_market_change = on_market_change
        self.on_market_end = on_market_end
        self.current: MarketStore | None = None
        self._token_up: str | None = None
        self._token_down: str | None = None

    @property
    def token_up(self) -> str | None:
        return self._token_up

    @property
    def token_down(self) -> str | None:
        return self._token_down

    async def _finalize_ending(self, store: MarketStore) -> None:
        if self.on_market_end is not None:
            try:
                await self.on_market_end(store)
            except Exception as exc:
                logger.exception(
                    "Market-end trades fetch failed for {}: {}", store.market_id, exc
                )
        store.update_meta(active=False)
        store.flush(force=True)

    async def tick(self) -> MarketStore | None:
        market = await self.discovery.discover_active()
        if market is None:
            if self.current and int(time.time() * 1000) >= self.current.end_ms:
                if self.current.meta.get("active", True):
                    await self._finalize_ending(self.current)
            return self.current

        market_id = str(market.get("id") or market.get("conditionId") or "")
        if not market_id:
            return self.current

        start_ms, end_ms = parse_window(market)
        token_up, token_down = parse_token_ids(market)

        if self.current and self.current.market_id == market_id:
            self._token_up = token_up
            self._token_down = token_down
            return self.current

        if self.current is not None:
            ending = self.current
            await self._finalize_ending(ending)
            logger.info("Rolled off market {}", ending.market_id)

        open_px = await self.twap.resolve_open_price(start_ms)
        condition_id = str(
            market.get("conditionId") or market.get("condition_id") or ""
        )
        meta = {
            "market_id": market_id,
            "condition_id": condition_id or None,
            "slug": str(market.get("slug") or ""),
            "question": str(market.get("question") or market.get("title") or ""),
            "up_token_id": token_up,
            "down_token_id": token_down,
            "start_time": start_ms,
            "end_time": end_ms,
            "resolved_at": None,
            "btc_open_price": open_px,
            "winner": None,
            "active": True,
            "closed": False,
        }
        store = MarketStore(meta)
        self.current = store
        self._token_up = token_up
        self._token_down = token_down
        logger.info(
            "New market {} slug={} open_twap={}",
            market_id,
            meta["slug"],
            open_px,
        )
        if self.on_market_change is not None:
            await self.on_market_change(store, token_up, token_down, condition_id)
        return store
