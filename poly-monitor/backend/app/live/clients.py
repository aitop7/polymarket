"""Thin public-API clients for live BTC Up/Down 5m markets."""

from __future__ import annotations

import json
from typing import Any

import httpx

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
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
        self._data = httpx.AsyncClient(base_url=DATA_API_URL, timeout=timeout)
        self._binance = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._gamma.aclose()
        await self._clob.aclose()
        await self._data.aclose()
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

    async def get_holders(
        self, condition_id: str, *, limit: int = 20, min_balance: int = 1
    ) -> list[dict[str, Any]]:
        """Top holders per outcome token from Polymarket Data API."""
        cid = str(condition_id or "").strip()
        if not cid:
            return []
        try:
            resp = await self._data.get(
                "/holders",
                params={
                    "market": cid,
                    "limit": max(1, min(20, int(limit))),
                    "minBalance": max(0, int(min_balance)),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

    async def get_btc_open_at(self, start_ms: int) -> float | None:
        """Best-effort open proxy: first Binance agg trade at/after window start."""
        for base in (BINANCE_URL, *BINANCE_FALLBACKS):
            try:
                resp = await self._binance.get(
                    f"{base.rstrip('/')}/api/v3/aggTrades",
                    params={
                        "symbol": "BTCUSDT",
                        "startTime": int(start_ms),
                        "endTime": int(start_ms) + 15_000,
                        "limit": 1,
                    },
                )
                resp.raise_for_status()
                rows = resp.json()
                if isinstance(rows, list) and rows:
                    return float(rows[0]["p"])
            except Exception:
                continue
        return None

    async def _agg_trades_range(
        self, base: str, start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        """Fetch aggTrades covering [start_ms, end_ms], paginating if needed."""
        out: list[dict[str, Any]] = []
        cursor = int(start_ms)
        end = int(end_ms)
        for _ in range(20):
            resp = await self._binance.get(
                f"{base.rstrip('/')}/api/v3/aggTrades",
                params={
                    "symbol": "BTCUSDT",
                    "startTime": cursor,
                    "endTime": end,
                    "limit": 1000,
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                break
            out.extend(rows)
            last_t = int(rows[-1]["T"])
            if last_t >= end or len(rows) < 1000:
                break
            cursor = last_t + 1
            if cursor > end:
                break
        return out

    async def compute_twap_30s_ending_at(self, end_ms: int) -> float | None:
        """
        Fallback only: time-weighted average of Binance BTCUSDT aggTrades
        over [end-30s, end] when RTDS missed the Chainlink 30s TWAP sample.

        Primary host: https://data-api.binance.vision/api/v3/aggTrades
        """
        end = int(end_ms)
        start = end - 30_000
        last_exc: Exception | None = None
        for base in (BINANCE_URL, *BINANCE_FALLBACKS):
            try:
                rows = await self._agg_trades_range(base, start, end)
                if not rows:
                    continue
                # Build (t_ms, price) series; TWAP = ∫ price dt / 30s
                points: list[tuple[int, float]] = []
                for row in rows:
                    try:
                        t = int(row["T"])
                        p = float(row["p"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if t < start or t > end:
                        continue
                    points.append((t, p))
                if not points:
                    continue
                points.sort(key=lambda x: x[0])
                # Hold first trade price from window start if first trade is late.
                if points[0][0] > start:
                    points.insert(0, (start, points[0][1]))
                weighted = 0.0
                for i, (t, p) in enumerate(points):
                    t_next = points[i + 1][0] if i + 1 < len(points) else end
                    dt = max(0, t_next - t)
                    weighted += p * dt
                duration = end - start
                if duration <= 0:
                    return points[-1][1]
                return weighted / duration
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            return None
        return None

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        resp = await self._clob.get("/book", params={"token_id": token_id})
        resp.raise_for_status()
        return resp.json()
