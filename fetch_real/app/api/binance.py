from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from app.config import settings
from app.utils.logger import logger
from app.utils.time import datetime_to_ms, ms_to_datetime


class BinanceClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=30.0)
        self._base_url = settings.binance_rest_url.rstrip("/")

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for base in settings.binance_rest_bases:
            url = f"{base}{path}"
            try:
                resp = await self.client.get(url, params=params)
                if resp.status_code in {403, 418, 451}:
                    logger.warning("Binance {} blocked status={} — trying fallback", base, resp.status_code)
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                    continue
                resp.raise_for_status()
                self._base_url = base
                logger.debug(
                    "Binance {} latency={}ms",
                    path,
                    int(resp.elapsed.total_seconds() * 1000),
                )
                return resp.json()
            except Exception as exc:
                last_error = exc
                logger.warning("Binance request failed on {}: {}", base, exc)
        assert last_error is not None
        raise last_error

    async def get_agg_trades(
        self,
        *,
        symbol: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        from_id: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "symbol": (symbol or settings.btc_symbol).upper(),
            "limit": min(limit, 1000),
        }
        if start_time is not None:
            params["startTime"] = datetime_to_ms(start_time)
        if end_time is not None:
            params["endTime"] = datetime_to_ms(end_time)
        if from_id is not None:
            params["fromId"] = from_id

        data = await self._get("/api/v3/aggTrades", params)
        return data

    async def iter_agg_trades(
        self,
        *,
        start_time: datetime,
        end_time: datetime,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        """Paginate aggTrades across the full [start, end] window."""
        out: list[dict[str, Any]] = []
        cursor = start_time
        end_ms = datetime_to_ms(end_time)
        safety = 0
        while cursor < end_time and safety < 500:
            safety += 1
            batch = await self.get_agg_trades(
                symbol=symbol,
                start_time=cursor,
                end_time=end_time,
                limit=1000,
            )
            if not batch:
                break
            out.extend(batch)
            last_ts = int(batch[-1]["T"])
            if last_ts >= end_ms or len(batch) < 1000:
                break
            # advance 1ms past last trade to avoid duplicates
            cursor = ms_to_datetime(last_ts + 1)
        # de-dupe by agg trade id
        seen: set[Any] = set()
        unique: list[dict[str, Any]] = []
        for t in out:
            key = t.get("a")
            if key in seen:
                continue
            seen.add(key)
            unique.append(t)
        return unique

    async def get_klines(
        self,
        *,
        symbol: str | None = None,
        interval: str = "1m",
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 1000,
    ) -> list[list[Any]]:
        params: dict[str, Any] = {
            "symbol": (symbol or settings.btc_symbol).upper(),
            "interval": interval,
            "limit": min(limit, 1000),
        }
        if start_time is not None:
            params["startTime"] = datetime_to_ms(start_time)
        if end_time is not None:
            params["endTime"] = datetime_to_ms(end_time)

        data = await self._get("/api/v3/klines", params)
        return data

    @staticmethod
    def normalize_agg_trade(
        trade: dict[str, Any],
        best_bid: float | None = None,
        best_ask: float | None = None,
    ) -> dict[str, Any]:
        # m=true => buyer is maker => aggressor sold
        side = "sell" if trade.get("m") else "buy"
        return {
            "timestamp": ms_to_datetime(trade["T"]),
            "price": float(trade["p"]),
            "size": float(trade["q"]),
            "side": side,
            "best_bid": best_bid,
            "best_ask": best_ask,
        }
