"""RTDS Chainlink spot + 30s TWAP; resolves meta open/close TWAP prices."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any

import httpx
import websockets
from loguru import logger

from app.config import settings

BINANCE_FALLBACKS = (
    "https://api.binance.com",
    "https://api1.binance.com",
)


class TwapOpenResolver:
    """RTDS Chainlink + 30s TWAP buffer; Binance historical TWAP fallback for open."""

    def __init__(self) -> None:
        self._twap_hist: deque[tuple[int, float]] = deque(maxlen=20_000)
        self._chainlink: deque[tuple[int, float]] = deque(maxlen=20_000)
        self._twap_price: float | None = None
        self._twap_ts: int | None = None
        self._chainlink_price: float | None = None
        self._chainlink_ts: int | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0))

    def ensure_started(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="rtds-btc-prices")

    async def close(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await self._http.aclose()

    def latest_chainlink(self) -> tuple[float, int] | None:
        if self._chainlink_price is None or self._chainlink_ts is None:
            return None
        return float(self._chainlink_price), int(self._chainlink_ts)

    def latest_twap(self) -> tuple[float, int] | None:
        if self._twap_price is None or self._twap_ts is None:
            return None
        return float(self._twap_price), int(self._twap_ts)

    def twap_at_close(self, end_ms: int, *, grace_ms: int = 5_000) -> tuple[float, int] | None:
        end = int(end_ms)
        lo, hi = end - grace_ms, end + grace_ms
        best: tuple[float, int] | None = None
        best_key: tuple[int, int] | None = None
        for ts, px in self._twap_hist:
            if ts < lo or ts > hi:
                continue
            delta = abs(ts - end)
            after = 0 if ts <= end else 1
            key = (delta, after)
            if best_key is None or key < best_key:
                best_key = key
                best = (float(px), int(ts))
        return best

    async def resolve_twap_at(self, at_ms: int, *, wait_s: float = 3.0) -> float | None:
        """30s Chainlink TWAP ending at at_ms (open=T0, close=T1)."""
        self.ensure_started()
        hit = self.twap_at_close(at_ms)
        if hit is None:
            now = int(time.time() * 1000)
            # Wait briefly if we are near the target timestamp
            if abs(now - at_ms) < 20_000 and wait_s > 0:
                deadline = time.monotonic() + wait_s
                while time.monotonic() < deadline and hit is None:
                    await asyncio.sleep(0.15)
                    hit = self.twap_at_close(at_ms)
        if hit is not None:
            return float(hit[0])
        return await self.compute_twap_30s_ending_at(at_ms)

    async def resolve_open_price(self, window_start_ms: int) -> float | None:
        """btc_open_price = 30s TWAP ending at window start (Price to Beat)."""
        return await self.resolve_twap_at(window_start_ms)

    async def resolve_close_price(self, window_end_ms: int) -> float | None:
        """btc_close_price = 30s TWAP ending at window end."""
        return await self.resolve_twap_at(window_end_ms, wait_s=5.0)

    async def compute_twap_30s_ending_at(self, end_ms: int) -> float | None:
        end = int(end_ms)
        start = end - 30_000
        for base in (settings.binance_rest_url, *BINANCE_FALLBACKS):
            try:
                points = await self._agg_points(base, start, end)
                if not points:
                    continue
                if points[0][0] > start:
                    points.insert(0, (start, points[0][1]))
                weighted = 0.0
                for i, (t, p) in enumerate(points):
                    t_next = points[i + 1][0] if i + 1 < len(points) else end
                    weighted += p * max(0, t_next - t)
                dur = end - start
                if dur <= 0:
                    return points[-1][1]
                return weighted / dur
            except Exception:
                continue
        return None

    async def _agg_points(
        self, base: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, float]]:
        out: list[tuple[int, float]] = []
        cursor = start_ms
        for _ in range(20):
            resp = await self._http.get(
                f"{base.rstrip('/')}/api/v3/aggTrades",
                params={
                    "symbol": settings.btc_symbol.upper(),
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                try:
                    t = int(row["T"])
                    p = float(row["p"])
                except (KeyError, TypeError, ValueError):
                    continue
                if start_ms <= t <= end_ms:
                    out.append((t, p))
            last_t = int(rows[-1]["T"])
            if last_t >= end_ms or len(rows) < 1000:
                break
            cursor = last_t + 1
        out.sort(key=lambda x: x[0])
        return out

    async def _run(self) -> None:
        while self._running:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("RTDS BTC prices error: {}", exc)
            if self._running:
                await asyncio.sleep(2)

    async def _session(self) -> None:
        sub = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "crypto_prices_twap_thirty",
                    "type": "update",
                    "filters": '{"symbol":"btc/usd"}',
                },
                {
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": '{"symbol":"btc/usd"}',
                },
            ],
        }
        async with websockets.connect(
            settings.rtds_url,
            ping_interval=None,
            ping_timeout=None,
            max_size=2**20,
        ) as ws:
            await ws.send(json.dumps(sub))
            logger.info("RTDS subscribed Chainlink + 30s TWAP (btc/usd)")
            ping_at = time.monotonic()
            while self._running:
                timeout = max(0.1, 5.0 - (time.monotonic() - ping_at))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    await ws.send("PING")
                    ping_at = time.monotonic()
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                if raw.strip() == "PONG":
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._handle(msg)

    def _handle(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        topic = str(msg.get("topic") or "")
        payload = msg.get("payload") or {}
        if not isinstance(payload, dict):
            return
        symbol = str(payload.get("symbol") or "").lower()
        if symbol and symbol not in {"btc/usd", "btcusdt"}:
            return
        value = payload.get("value")
        raw_full = payload.get("full_accuracy_value")
        if raw_full is not None:
            try:
                value = float(raw_full) / 1e18
            except (TypeError, ValueError):
                pass
        try:
            price = float(value) if value is not None else None
        except (TypeError, ValueError):
            price = None
        if price is None:
            return
        try:
            ts_ms = int(payload.get("timestamp") or time.time() * 1000)
        except (TypeError, ValueError):
            ts_ms = int(time.time() * 1000)

        if topic in {"crypto_prices_twap_thirty", "prices.crypto.chainlink.twap"}:
            window = (
                payload.get("window_s")
                or payload.get("windowSeconds")
                or payload.get("window_seconds")
            )
            if window is not None and int(window) != 30:
                return
            self._twap_price = price
            self._twap_ts = ts_ms
            self._twap_hist.append((ts_ms, price))
            return

        if topic in {"crypto_prices_chainlink", "prices.crypto.chainlink"}:
            self._chainlink_price = price
            self._chainlink_ts = ts_ms
            self._chainlink.append((ts_ms, price))
