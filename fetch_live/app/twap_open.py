"""Previous-market close 30s TWAP for meta.btc_open_price."""

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
    """RTDS 30s TWAP buffer + Binance historical TWAP fallback."""

    def __init__(self) -> None:
        self._twap_hist: deque[tuple[int, float]] = deque(maxlen=20_000)
        self._price: float | None = None
        self._ts: int | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0))

    def ensure_started(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="rtds-twap")

    async def close(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await self._http.aclose()

    def twap_at_close(self, end_ms: int, *, grace_ms: int = 5_000) -> tuple[float, int] | None:
        end = int(end_ms)
        lo, hi = end - grace_ms, end + grace_ms
        best: tuple[float, int] | None = None
        best_key: tuple[int, int] | None = None
        for ts, px in self._twap_hist:
            if ts < lo or ts > hi:
                continue
            after = 0 if ts <= end else 1
            key = (after, abs(ts - end))
            if best_key is None or key < best_key:
                best_key = key
                best = (float(px), int(ts))
        return best

    async def resolve_open_price(self, window_start_ms: int) -> float | None:
        """btc_open_price = previous market close 30s TWAP at T0."""
        self.ensure_started()
        hit = self.twap_at_close(window_start_ms)
        if hit is None:
            # brief wait if near open
            now = int(time.time() * 1000)
            if now - window_start_ms < 20_000:
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and hit is None:
                    await asyncio.sleep(0.15)
                    hit = self.twap_at_close(window_start_ms)
        if hit is not None:
            return float(hit[0])
        return await self.compute_twap_30s_ending_at(window_start_ms)

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
                logger.warning("RTDS TWAP error: {}", exc)
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
                }
            ],
        }
        async with websockets.connect(
            settings.rtds_url,
            ping_interval=None,
            ping_timeout=None,
            max_size=2**20,
        ) as ws:
            await ws.send(json.dumps(sub))
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
        if topic not in {"crypto_prices_twap_thirty", "prices.crypto.chainlink.twap"}:
            return
        payload = msg.get("payload") or {}
        if not isinstance(payload, dict):
            return
        symbol = str(payload.get("symbol") or "").lower()
        if symbol and symbol not in {"btc/usd", "btcusdt"}:
            return
        window = payload.get("window_s") or payload.get("windowSeconds")
        if window is not None and int(window) != 30:
            return
        value = payload.get("value")
        if value is None and payload.get("full_accuracy_value") is not None:
            try:
                value = float(payload["full_accuracy_value"]) / 1e18
            except (TypeError, ValueError):
                value = None
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
        self._price = price
        self._ts = ts_ms
        self._twap_hist.append((ts_ms, price))
