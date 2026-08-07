"""Post-window resolution polling for meta.winner / closed."""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger

from app.discovery import Discovery
from app.storage.market_store import MarketStore


def _parse_winner(market: dict[str, Any]) -> bool | None:
    """true=Up won, false=Down won, None=unresolved."""
    if not bool(market.get("closed") or market.get("resolved")):
        # still open
        prices = market.get("outcomePrices") or market.get("outcome_prices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                prices = None
        if isinstance(prices, list) and len(prices) >= 2:
            try:
                up_p = float(prices[0])
                down_p = float(prices[1])
            except (TypeError, ValueError):
                return None
            if up_p >= 0.99 and down_p <= 0.01:
                return True
            if down_p >= 0.99 and up_p <= 0.01:
                return False
        return None
    # closed
    prices = market.get("outcomePrices") or market.get("outcome_prices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            prices = None
    if isinstance(prices, list) and len(prices) >= 1:
        try:
            up_p = float(prices[0])
            if up_p >= 0.5:
                return True
            return False
        except (TypeError, ValueError):
            pass
    winner = market.get("winner") or market.get("winningOutcome")
    if winner is not None:
        w = str(winner).strip().lower()
        if w in {"up", "yes", "0", "true"}:
            return True
        if w in {"down", "no", "1", "false"}:
            return False
    return None


class Resolver:
    def __init__(self, discovery: Discovery) -> None:
        self.discovery = discovery
        self._pending: dict[str, MarketStore] = {}

    def track(self, store: MarketStore) -> None:
        self._pending[store.market_id] = store

    async def poll_once(self) -> None:
        done: list[str] = []
        now = int(time.time() * 1000)
        for mid, store in list(self._pending.items()):
            if now < store.end_ms:
                continue
            market = await self.discovery.get_market_by_slug(str(store.meta.get("slug") or ""))
            if market is None:
                market = await self.discovery.get_market_by_id(mid)
            if market is None:
                continue
            closed = bool(market.get("closed"))
            winner = _parse_winner(market)
            if winner is None and not closed:
                continue
            patch: dict[str, Any] = {
                "winner": winner,
                "resolved_at": int(time.time() * 1000)
                if winner is not None or closed
                else None,
                "active": False,
                "closed": closed or winner is not None,
            }
            store.update_meta(**patch)
            store.flush(force=True)
            logger.info(
                "Resolved market {} winner={} closed={} open={} close={}",
                mid,
                winner,
                closed,
                store.meta.get("btc_open_price"),
                store.meta.get("btc_close_price"),
            )
            if closed or winner is not None:
                done.append(mid)
        for mid in done:
            self._pending.pop(mid, None)
