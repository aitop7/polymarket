from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import settings
from app.utils.logger import logger
from app.utils.time import ms_to_datetime


class PolymarketClient:
    def __init__(
        self,
        gamma_client: httpx.AsyncClient | None = None,
        clob_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_gamma = gamma_client is None
        self._owns_clob = clob_client is None
        self._owns_data = True
        self.gamma = gamma_client or httpx.AsyncClient(
            base_url=settings.polymarket_gamma_url,
            timeout=30.0,
        )
        self.clob = clob_client or httpx.AsyncClient(
            base_url=settings.polymarket_clob_url,
            timeout=30.0,
        )
        self.data = httpx.AsyncClient(
            base_url=settings.polymarket_data_api_url,
            timeout=30.0,
        )

    async def close(self) -> None:
        if self._owns_gamma:
            await self.gamma.aclose()
        if self._owns_clob:
            await self.clob.aclose()
        if self._owns_data:
            await self.data.aclose()

    async def list_markets(
        self,
        *,
        slug_contains: str | None = None,
        active: bool | None = True,
        closed: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()

        resp = await self.gamma.get("/markets", params=params)
        resp.raise_for_status()
        markets = resp.json()
        logger.debug(
            "Gamma /markets latency={}ms count={}",
            int(resp.elapsed.total_seconds() * 1000),
            len(markets),
        )

        if slug_contains:
            needle = slug_contains.lower()
            markets = [m for m in markets if needle in str(m.get("slug", "")).lower()]
        return markets

    async def search_events(self, query: str, *, limit_per_page: int = 20) -> list[dict[str, Any]]:
        resp = await self.gamma.get("/public-search", params={"q": query})
        resp.raise_for_status()
        payload = resp.json()
        events = payload.get("events", []) if isinstance(payload, dict) else []
        logger.debug(
            "Gamma /public-search q={} latency={}ms events={}",
            query,
            int(resp.elapsed.total_seconds() * 1000),
            len(events),
        )
        return events[:limit_per_page]

    async def discover_btc_markets(self, *, queries: list[str] | None = None) -> list[dict[str, Any]]:
        """Find BTC 5m up/down markets via search + slug filter."""
        from app.utils.markets import filter_updown_rows

        queries = queries or [
            "btc updown",
            "bitcoin up or down",
            settings.market_slug_prefix,
            "btc-updown-5m",
        ]
        by_id: dict[str, dict[str, Any]] = {}

        for query in queries:
            try:
                events = await self.search_events(query)
            except Exception as exc:
                logger.warning("Search failed for {}: {}", query, exc)
                continue
            for event in events:
                for market in event.get("markets") or []:
                    mid = str(market.get("id") or market.get("conditionId") or "")
                    if mid:
                        by_id[mid] = market

        try:
            listed = await self.list_markets(active=True, closed=False, limit=100)
            for market in listed:
                slug = str(market.get("slug", "")).lower()
                if settings.market_slug_prefix.lower() in slug:
                    mid = str(market.get("id") or market.get("conditionId") or "")
                    if mid:
                        by_id[mid] = market
        except Exception as exc:
            logger.warning("Market list scan failed: {}", exc)

        rows = [self.normalize_market(m) for m in by_id.values()]
        return filter_updown_rows(rows)

    async def get_market_by_slug(self, slug: str) -> dict[str, Any] | None:
        """
        Resolve a market by slug.

        Closed 5m markets are invisible to bare /markets?slug= — try /events first,
        then /markets with closed=true/false.
        """
        # 1) Events endpoint (works for both open and closed up/down markets)
        try:
            resp = await self.gamma.get("/events", params={"slug": slug})
            resp.raise_for_status()
            events = resp.json()
            if isinstance(events, list) and events:
                event_markets = events[0].get("markets") or []
                for market in event_markets:
                    if str(market.get("slug") or "") == slug or len(event_markets) == 1:
                        return market
                if event_markets:
                    return event_markets[0]
        except Exception as exc:
            logger.debug("events slug lookup failed {}: {}", slug, exc)

        # 2) Markets endpoint — closed and open variants
        for params in (
            {"slug": slug, "closed": "true"},
            {"slug": slug, "closed": "false"},
            {"slug": slug},
        ):
            try:
                resp = await self.gamma.get("/markets", params=params)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list) and data:
                    return data[0]
                if isinstance(data, dict) and data:
                    return data
            except Exception as exc:
                logger.debug("markets slug lookup failed {} {}: {}", slug, params, exc)
        return None

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        resp = await self.clob.get("/book", params={"token_id": token_id})
        resp.raise_for_status()
        data = resp.json()
        logger.debug(
            "CLOB /book latency={}ms token={}",
            int(resp.elapsed.total_seconds() * 1000),
            token_id[:12],
        )
        return data

    async def get_price_history(
        self,
        token_id: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
        fidelity: int = 1,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"market": token_id, "fidelity": fidelity}
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts

        resp = await self.clob.get("/prices-history", params=params)
        resp.raise_for_status()
        payload = resp.json()
        history = payload.get("history", payload) if isinstance(payload, dict) else payload
        logger.debug(
            "CLOB /prices-history latency={}ms points={}",
            int(resp.elapsed.total_seconds() * 1000),
            len(history) if isinstance(history, list) else 0,
        )
        return history if isinstance(history, list) else []

    async def get_trades(
        self,
        condition_id: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
        limit: int = 500,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Historical trades from Polymarket data-api (works for closed 5m markets).
        Filter by condition id via market=...
        """
        if not condition_id:
            return []
        out: list[dict[str, Any]] = []
        offset = 0
        for _ in range(max_pages):
            resp = await self.data.get(
                "/trades",
                params={"market": condition_id, "limit": limit, "offset": offset},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            for trade in batch:
                ts = int(trade.get("timestamp") or 0)
                if start_ts is not None and ts < start_ts:
                    continue
                if end_ts is not None and ts > end_ts:
                    continue
                out.append(trade)
            if len(batch) < limit:
                break
            offset += limit
            # data-api returns newest-first; stop if entire page is before window
            oldest = min(int(t.get("timestamp") or 0) for t in batch)
            if start_ts is not None and oldest < start_ts and all(
                int(t.get("timestamp") or 0) < start_ts for t in batch
            ):
                break
        logger.debug(
            "data-api /trades condition={} points={} window={}-{}",
            condition_id[:16],
            len(out),
            start_ts,
            end_ts,
        )
        return out

    @staticmethod
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

    @staticmethod
    def parse_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:
                return ms_to_datetime(ts)
            return datetime.fromtimestamp(ts, tz=UTC)
        text = str(value).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt

    def normalize_market(self, market: dict[str, Any]) -> dict[str, Any]:
        token_yes, token_no = self.parse_token_ids(market)
        market_id = str(market.get("id") or market.get("conditionId") or market.get("condition_id"))
        condition_id = market.get("conditionId") or market.get("condition_id")
        closed = bool(market.get("closed"))
        active = bool(market.get("active", True))
        if closed:
            status = "closed"
        elif active:
            status = "active"
        else:
            status = "inactive"

        winner = None
        if market.get("umaResolutionStatus") == "resolved" or closed:
            winner = market.get("winningOutcome") or market.get("winner")

        return {
            "market_id": market_id,
            "slug": str(market.get("slug") or market_id),
            "condition_id": str(condition_id) if condition_id else None,
            "token_yes": token_yes,
            "token_no": token_no,
            "start_time": self.parse_datetime(market.get("eventStartTime") or market.get("startDate")),
            "end_time": self.parse_datetime(market.get("endDate") or market.get("end_date_iso")),
            "settlement_time": self.parse_datetime(
                market.get("closedTime") or market.get("umaEndDate") or market.get("endDate")
            ),
            "opening_btc_price": None,
            "closing_btc_price": None,
            "winner": str(winner) if winner else None,
            "status": status,
            "raw_json": market,
        }
