"""Thin public-API clients for live BTC Up/Down 5m markets."""

from __future__ import annotations

import json
from typing import Any

import httpx

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
BINANCE_URL = "https://data-api.binance.vision"
BINANCE_FALLBACKS = (
    "https://api.binance.com",
    "https://api1.binance.com",
)

MARKET_DURATION_S = 300


def window_start_unix(now_s: float | None = None) -> int:
    import time

    ts = int(now_s if now_s is not None else time.time())
    return ts - (ts % MARKET_DURATION_S)


def parse_token_ids(market: dict[str, Any]) -> tuple[str | None, str | None]:
    raw = market.get("clobTokenIds") or market.get("clob_token_ids")
    if raw is None:
        return None, None
    if isinstance(raw, str):
        try:
            ids = json.loads(raw)
        except json.JSONDecodeError:
            ids = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        ids = list(raw)
    yes = str(ids[0]) if len(ids) > 0 else None
    no = str(ids[1]) if len(ids) > 1 else None
    return yes, no


class LiveClients:
    def __init__(self) -> None:
        timeout = httpx.Timeout(5.0, connect=3.0)
        self._gamma = httpx.AsyncClient(base_url=GAMMA_URL, timeout=timeout)
        self._clob = httpx.AsyncClient(base_url=CLOB_URL, timeout=timeout)
        self._binance = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._gamma.aclose()
        await self._clob.aclose()
        await self._binance.aclose()

    async def get_btc_price(self) -> float:
        last_exc: Exception | None = None
        for base in (BINANCE_URL, *BINANCE_FALLBACKS):
            try:
                resp = await self._binance.get(
                    f"{base.rstrip('/')}/api/v3/ticker/price",
                    params={"symbol": "BTCUSDT"},
                )
                resp.raise_for_status()
                return float(resp.json()["price"])
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"Binance BTC price failed: {last_exc}")

    async def get_market_by_slug(self, slug: str) -> dict[str, Any] | None:
        try:
            resp = await self._gamma.get("/events", params={"slug": slug})
            resp.raise_for_status()
            events = resp.json()
            if isinstance(events, list) and events:
                markets = events[0].get("markets") or []
                for market in markets:
                    if str(market.get("slug") or "") == slug or len(markets) == 1:
                        return market
                if markets:
                    return markets[0]
        except Exception:
            return None
        return None

    async def discover_active_updown(self) -> dict[str, Any] | None:
        """Resolve current (or nearest) open btc-updown-5m market."""
        start = window_start_unix()
        # Prefer current window, then previous/next (clock skew / rollover).
        for offset in (0, -MARKET_DURATION_S, MARKET_DURATION_S, -2 * MARKET_DURATION_S):
            slug = f"btc-updown-5m-{start + offset}"
            try:
                market = await self.get_market_by_slug(slug)
            except Exception:
                continue
            if not market:
                continue
            if bool(market.get("closed")):
                continue
            return market
        return None

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        resp = await self._clob.get("/book", params={"token_id": token_id})
        resp.raise_for_status()
        return resp.json()
