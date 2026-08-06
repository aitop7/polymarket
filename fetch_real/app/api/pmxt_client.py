from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Any

from app.config import settings
from app.utils.logger import logger
from app.utils.time import ms_to_datetime


class PmxtClient:
    """
    Async wrapper around the sync PMXT SDK.

    Uses a thread-local SDK client so asyncio.to_thread calls can run in parallel
    safely across a thread pool.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (api_key if api_key is not None else settings.pmxt_api_key or "").strip()
        self._local = threading.local()
        self._clients: list[Any] = []
        self._clients_lock = threading.Lock()
        self._sem = asyncio.Semaphore(max(1, settings.download_concurrency))

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _thread_client(self) -> Any:
        if not self.enabled:
            raise RuntimeError("PMXT_API_KEY is not configured")
        client = getattr(self._local, "client", None)
        if client is None:
            import pmxt

            client = pmxt.Polymarket(pmxt_api_key=self.api_key)
            self._local.client = client
            with self._clients_lock:
                self._clients.append(client)
            logger.debug("PMXT thread client created id={}", id(client))
        return client

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        async with self._sem:
            return await asyncio.to_thread(fn, *args, **kwargs)

    async def close(self) -> None:
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                await asyncio.to_thread(client.close)
            except Exception as exc:
                logger.debug("PMXT close: {}", exc)

    async def fetch_markets(
        self,
        *,
        query: str | None = None,
        slug: str | None = None,
        limit: int = 50,
        sort: str | None = "volume",
        status: str | None = "active",
    ) -> list[Any]:
        def _call() -> list[Any]:
            client = self._thread_client()
            params: dict[str, Any] = {"limit": limit}
            if query:
                params["query"] = query
            if slug:
                params["slug"] = slug
            if sort:
                params["sort"] = sort
            if status:
                params["status"] = status
            return client.fetch_markets(params)

        return await self._run(_call)

    async def discover_btc_markets(self) -> list[dict[str, Any]]:
        from app.utils.markets import filter_updown_rows

        queries = [
            "btc updown",
            "bitcoin up or down",
            settings.market_slug_prefix,
            "btc-updown-5m",
        ]

        async def _one(query: str) -> list[dict[str, Any]]:
            try:
                found = await self.fetch_markets(query=query, limit=100, sort="newest")
                return [self.normalize_market(m) for m in found]
            except Exception as exc:
                logger.warning("PMXT fetch_markets failed q={}: {}", query, exc)
                return []

        batches = await asyncio.gather(*[_one(q) for q in queries])
        by_id: dict[str, dict[str, Any]] = {}
        for batch in batches:
            for row in batch:
                by_id[row["market_id"]] = row
        return filter_updown_rows(list(by_id.values()))

    async def fetch_order_book(
        self,
        outcome_id: str,
        *,
        since: int | None = None,
        until: int | None = None,
        outcome: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        def _call() -> Any:
            client = self._thread_client()
            params: dict[str, Any] = {}
            if since is not None:
                params["since"] = since
            if until is not None:
                params["until"] = until
            if outcome is not None:
                params["outcome"] = outcome
            kwargs: dict[str, Any] = {}
            if limit is not None:
                kwargs["limit"] = limit
            if params:
                kwargs["params"] = params
            return client.fetch_order_book(outcome_id, **kwargs)

        result = await self._run(_call)
        if isinstance(result, list):
            return [self.orderbook_to_dict(b) for b in result]
        return self.orderbook_to_dict(result)

    async def fetch_ohlcv(
        self,
        outcome_id: str,
        *,
        resolution: str = "1m",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        def _call() -> list[Any]:
            client = self._thread_client()
            return client.fetch_ohlcv(
                outcome_id,
                resolution=resolution,
                start=start,
                end=end,
                limit=limit,
            )

        candles = await self._run(_call)
        out: list[dict[str, Any]] = []
        for c in candles:
            out.append(
                {
                    "timestamp": getattr(c, "timestamp", None),
                    "open": float(getattr(c, "open", 0)),
                    "high": float(getattr(c, "high", 0)),
                    "low": float(getattr(c, "low", 0)),
                    "close": float(getattr(c, "close", 0)),
                    "volume": float(getattr(c, "volume", 0) or 0),
                }
            )
        return out

    async def fetch_trades(
        self,
        outcome_id: str,
        *,
        limit: int = 200,
        since: int | None = None,
        start: datetime | int | str | None = None,
        end: datetime | int | str | None = None,
    ) -> list[dict[str, Any]]:
        def _call() -> list[Any]:
            client = self._thread_client()
            return client.fetch_trades(
                outcome_id,
                limit=limit,
                since=since,
                start=start,
                end=end,
            )

        trades = await self._run(_call)
        out: list[dict[str, Any]] = []
        for t in trades:
            ts = getattr(t, "timestamp", None)
            out.append(
                {
                    "trade_id": str(getattr(t, "id", "")),
                    "timestamp": ms_to_datetime(ts) if ts is not None else None,
                    "price": float(getattr(t, "price", 0)),
                    "size": float(getattr(t, "amount", 0)),
                    "side": str(getattr(t, "side", "unknown")).lower(),
                }
            )
        return out

    @staticmethod
    def orderbook_to_dict(book: Any) -> dict[str, Any]:
        def _levels(levels: Any) -> list[dict[str, float]]:
            out: list[dict[str, float]] = []
            for level in levels or []:
                if hasattr(level, "price"):
                    out.append({"price": float(level.price), "size": float(level.size)})
                elif isinstance(level, dict):
                    out.append(
                        {
                            "price": float(level.get("price") or 0),
                            "size": float(level.get("size") or 0),
                        }
                    )
            return out

        ts = getattr(book, "timestamp", None)
        return {
            "bids": _levels(getattr(book, "bids", None)),
            "asks": _levels(getattr(book, "asks", None)),
            "timestamp": ts,
            "datetime": getattr(book, "datetime", None),
            "last_trade_price": getattr(book, "last_trade_price", None),
        }

    @staticmethod
    def normalize_market(market: Any) -> dict[str, Any]:
        yes = getattr(market, "yes", None) or getattr(market, "up", None)
        no = getattr(market, "no", None) or getattr(market, "down", None)
        outcomes = list(getattr(market, "outcomes", None) or [])
        if yes is None and outcomes:
            yes = outcomes[0]
        if no is None and len(outcomes) > 1:
            no = outcomes[1]

        meta = getattr(market, "source_metadata", None) or {}
        condition_id = (
            meta.get("conditionId")
            or meta.get("condition_id")
            or getattr(market, "contract_address", None)
        )
        market_id = str(getattr(market, "market_id", None) or condition_id or "")
        status_raw = str(getattr(market, "status", None) or "active").lower()
        if status_raw in {"closed", "resolved", "inactive"}:
            status = "closed" if status_raw != "inactive" else "inactive"
        else:
            status = "active"

        resolution = getattr(market, "resolution_date", None)
        slug = getattr(market, "slug", None) or market_id

        return {
            "market_id": market_id,
            "slug": str(slug),
            "condition_id": str(condition_id) if condition_id else None,
            "token_yes": str(getattr(yes, "outcome_id", None)) if yes else None,
            "token_no": str(getattr(no, "outcome_id", None)) if no else None,
            "start_time": None,
            "end_time": resolution if isinstance(resolution, datetime) else None,
            "settlement_time": resolution if isinstance(resolution, datetime) else None,
            "opening_btc_price": None,
            "closing_btc_price": None,
            "winner": None,
            "status": status,
            "raw_json": {
                "title": getattr(market, "title", None),
                "volume_24h": getattr(market, "volume_24h", None),
                "liquidity": getattr(market, "liquidity", None),
                "url": getattr(market, "url", None),
                "category": getattr(market, "category", None),
                "tags": getattr(market, "tags", None),
                "source_exchange": getattr(market, "source_exchange", None),
                "source_metadata": meta,
            },
        }
