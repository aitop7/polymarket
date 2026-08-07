"""Active market session lifecycle."""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from app.discovery import Discovery, parse_token_ids, parse_window
from app.storage.market_store import MarketStore
from app.twap_open import TwapOpenResolver


class SessionManager:
    def __init__(
        self,
        discovery: Discovery,
        twap: TwapOpenResolver,
        *,
        on_market_change: Any | None = None,
    ) -> None:
        self.discovery = discovery
        self.twap = twap
        self.on_market_change = on_market_change
        self.current: MarketStore | None = None
        self._token_up: str | None = None
        self._token_down: str | None = None

    @property
    def token_up(self) -> str | None:
        return self._token_up

    @property
    def token_down(self) -> str | None:
        return self._token_down

    async def tick(self) -> MarketStore | None:
        market = await self.discovery.discover_active()
        if market is None:
            # If wall clock moved past current end, deactivate
            if self.current and int(time.time() * 1000) >= self.current.end_ms:
                self.current.update_meta(active=False)
                self.current.flush(force=True)
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

        # Rollover
        if self.current is not None:
            self.current.update_meta(active=False)
            self.current.flush(force=True)
            logger.info("Rolled off market {}", self.current.market_id)

        open_px = await self.twap.resolve_open_price(start_ms)
        meta = {
            "market_id": market_id,
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
            await self.on_market_change(store, token_up, token_down)
        return store
