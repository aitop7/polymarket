"""Polymarket Data API trades — sole source for trades.parquet (includes proxyWallet)."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx
from loguru import logger

from app.config import settings

OnTradeRows = Callable[[list[dict[str, Any]]], None]


class DataApiTrades:
    def __init__(self, *, on_trades: OnTradeRows | None = None) -> None:
        self.on_trades = on_trades
        self._http = httpx.AsyncClient(
            base_url=settings.data_api_url,
            timeout=httpx.Timeout(15.0, connect=8.0),
        )
        self._running = False
        self._condition_id: str | None = None
        self._token_up: str | None = None
        self._token_down: str | None = None
        self._start_ms: int | None = None
        self._end_ms: int | None = None

    def set_market(
        self,
        *,
        condition_id: str | None,
        token_up: str | None,
        token_down: str | None,
        start_ms: int,
        end_ms: int,
    ) -> None:
        self._condition_id = condition_id or None
        self._token_up = token_up
        self._token_down = token_down
        self._start_ms = int(start_ms)
        self._end_ms = int(end_ms)

    async def close(self) -> None:
        self._running = False
        await self._http.aclose()

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.poll_once()
            except Exception as exc:
                logger.warning("Data API trades poll failed: {}", exc)
            await asyncio.sleep(settings.trades_poll_interval_s)

    async def poll_once(self) -> None:
        if not self._condition_id or not self.on_trades:
            return
        rows = await self.fetch_window(
            condition_id=self._condition_id,
            token_up=self._token_up,
            token_down=self._token_down,
            start_ms=self._start_ms or 0,
            end_ms=self._end_ms or 0,
            max_pages=5,
        )
        if rows:
            self.on_trades(rows)

    async def fetch_window(
        self,
        *,
        condition_id: str,
        token_up: str | None,
        token_down: str | None,
        start_ms: int,
        end_ms: int,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch Data API trades for a market window (newest-first pages)."""
        if not condition_id:
            return []
        out: list[dict[str, Any]] = []
        offset = 0
        for _ in range(max_pages):
            batch: list[Any] | None = None
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    resp = await self._http.get(
                        "/trades",
                        params={
                            "market": condition_id,
                            "limit": 500,
                            "offset": offset,
                        },
                    )
                    resp.raise_for_status()
                    raw = resp.json()
                    batch = raw if isinstance(raw, list) else []
                    break
                except Exception as exc:
                    last_exc = exc
                    await asyncio.sleep(0.4 * (attempt + 1))
            if batch is None:
                raise last_exc or RuntimeError("Data API fetch failed")
            if not batch:
                break
            for trade in batch:
                row = self._to_row(
                    trade,
                    token_up=token_up,
                    token_down=token_down,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
                if row is not None:
                    out.append(row)
            oldest = min(int(t.get("timestamp") or 0) for t in batch)
            if oldest and oldest < 10_000_000_000:
                oldest *= 1000
            # Stop once pages fall more than 5m before official open.
            too_old = bool(start_ms and oldest < start_ms - 300_000)
            if len(batch) < 500 or too_old:
                break
            offset += 500
        return out

    def _to_row(
        self,
        trade: dict[str, Any],
        *,
        token_up: str | None,
        token_down: str | None,
        start_ms: int,
        end_ms: int,
    ) -> dict[str, Any] | None:
        tx = str(trade.get("transactionHash") or trade.get("transaction_hash") or "")
        try:
            ts = int(trade.get("timestamp") or 0)
        except (TypeError, ValueError):
            return None
        if ts < 10_000_000_000:
            ts *= 1000
        # Allow pre-open prints (same conditionId); reject post-end and absurdly early.
        if end_ms and ts >= end_ms:
            return None
        if start_ms and ts < start_ms - 300_000:
            return None

        asset = str(trade.get("asset") or trade.get("asset_id") or "")
        is_down: bool | None = None
        if token_up and asset == str(token_up):
            is_down = False
        elif token_down and asset == str(token_down):
            is_down = True
        else:
            outcome = str(trade.get("outcome") or "").strip().lower()
            if outcome in {"up", "yes"}:
                is_down = False
            elif outcome in {"down", "no"}:
                is_down = True
            else:
                try:
                    is_down = int(trade.get("outcomeIndex")) == 1
                except (TypeError, ValueError):
                    return None
        if is_down is None:
            return None

        side_raw = str(trade.get("side") or "BUY").upper()
        side = side_raw in {"SELL", "S"}
        try:
            price = float(trade.get("price") or 0)
            size = float(trade.get("size") or 0)
        except (TypeError, ValueError):
            return None

        wallet = str(
            trade.get("proxyWallet")
            or trade.get("proxy_wallet")
            or trade.get("wallet")
            or ""
        )
        return {
            "timestamp": ts,
            "wallet": wallet,
            "token": bool(is_down),
            "side": bool(side),
            "price": price,
            "shares": max(0, min(int(round(size)), 2**32 - 1)),
            "transaction_hash": tx,
        }
