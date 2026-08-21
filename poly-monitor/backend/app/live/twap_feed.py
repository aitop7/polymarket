"""Polymarket RTDS: Chainlink spot + 30s/60s TWAP (per-asset feeds)."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any

import websockets

RTDS_URL = "wss://ws-live-data.polymarket.com"


def _subscribe_payload(rtds_symbol: str) -> dict[str, Any]:
    sym = str(rtds_symbol or "btc/usd").strip().lower() or "btc/usd"
    filt = json.dumps({"symbol": sym}, separators=(",", ":"))
    return {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_twap_thirty",
                "type": "update",
                "filters": filt,
            },
            {
                "topic": "crypto_prices_twap_sixty",
                "type": "update",
                "filters": filt,
            },
            {
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": filt,
            },
        ],
    }


# Accept TWAP samples within this window of a boundary (open/close).
_BOUNDARY_GRACE_MS = 5_000
_DEFAULT_LOOKBACK_S = 60


class TwapFeed:
    """Background RTDS subscriber for TWAP (30s + 60s) + Chainlink spot buffer."""

    def __init__(self, rtds_symbol: str = "btc/usd") -> None:
        self.rtds_symbol = str(rtds_symbol or "btc/usd").strip().lower() or "btc/usd"
        self._allowed_symbols = {
            self.rtds_symbol,
            self.rtds_symbol.replace("/", ""),
        }
        self._lookback_s = _DEFAULT_LOOKBACK_S
        self._twap_30: float | None = None
        self._twap_30_ts: int | None = None
        self._twap_60: float | None = None
        self._twap_60_ts: int | None = None
        self._error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        # Ring buffers: (timestamp_ms, price)
        self._chainlink: deque[tuple[int, float]] = deque(maxlen=20_000)
        self._twap_30_hist: deque[tuple[int, float]] = deque(maxlen=20_000)
        self._twap_60_hist: deque[tuple[int, float]] = deque(maxlen=20_000)

    def set_lookback_seconds(self, seconds: int | None) -> None:
        """Prefer 30s or 60s TWAP for Current Price / open-lock fallbacks."""
        try:
            s = int(seconds) if seconds is not None else _DEFAULT_LOOKBACK_S
        except (TypeError, ValueError):
            s = _DEFAULT_LOOKBACK_S
        self._lookback_s = 60 if s >= 45 else 30

    @property
    def lookback_seconds(self) -> int:
        return int(self._lookback_s)

    def _active_price(self) -> float | None:
        if self._lookback_s >= 45:
            return self._twap_60 if self._twap_60 is not None else self._twap_30
        return self._twap_30 if self._twap_30 is not None else self._twap_60

    def _active_ts(self) -> int | None:
        if self._lookback_s >= 45:
            if self._twap_60 is not None:
                return self._twap_60_ts
            return self._twap_30_ts
        if self._twap_30 is not None:
            return self._twap_30_ts
        return self._twap_60_ts

    def _active_hist(self) -> deque[tuple[int, float]]:
        if self._lookback_s >= 45 and self._twap_60_hist:
            return self._twap_60_hist
        if self._lookback_s < 45 and self._twap_30_hist:
            return self._twap_30_hist
        return self._twap_60_hist if self._twap_60_hist else self._twap_30_hist

    @property
    def price(self) -> float | None:
        return self._active_price()

    @property
    def timestamp_ms(self) -> int | None:
        return self._active_ts()

    @property
    def error(self) -> str | None:
        return self._error

    def latest(self) -> dict[str, Any]:
        return {
            # Keep legacy key; value follows the active lookback (30 or 60).
            "btc_twap_30s": self._active_price(),
            "btc_twap_ts": self._active_ts(),
            "btc_twap_lookback_s": self._lookback_s,
            "btc_twap_error": self._error,
            "btc_chainlink": self._chainlink[-1][1] if self._chainlink else None,
            "btc_chainlink_ts": self._chainlink[-1][0] if self._chainlink else None,
        }

    def history_since(self, start_ms: int) -> dict[str, list[tuple[int, float]]]:
        """Active TWAP + Chainlink samples with timestamp >= start_ms."""
        start = int(start_ms)
        hist = self._active_hist()
        return {
            "twap": [(int(ts), float(px)) for ts, px in hist if ts >= start],
            "chainlink": [(int(ts), float(px)) for ts, px in self._chainlink if ts >= start],
        }

    def chainlink_at_or_after(self, window_start_ms: int) -> tuple[float, int] | None:
        """First Chainlink spot tick with timestamp >= window open."""
        start = int(window_start_ms)
        for ts, px in self._chainlink:
            if ts >= start:
                return float(px), int(ts)
        return None

    def twap_at_close(
        self, window_end_ms: int, *, grace_ms: int = _BOUNDARY_GRACE_MS
    ) -> tuple[float, int] | None:
        """Active TWAP nearest to a boundary timestamp (open or close)."""
        return self.twap_nearest(window_end_ms, grace_ms=grace_ms)

    def twap_at_open(
        self, window_start_ms: int, *, grace_ms: int = _BOUNDARY_GRACE_MS
    ) -> tuple[float, int] | None:
        """Active TWAP nearest to market open."""
        return self.twap_nearest(window_start_ms, grace_ms=grace_ms)

    def twap_nearest(
        self, at_ms: int, *, grace_ms: int = _BOUNDARY_GRACE_MS
    ) -> tuple[float, int] | None:
        """
        Sample closest to at_ms within grace.

        Tie-break: prefer at/before the boundary (matches prior-window close continuity).
        """
        target = int(at_ms)
        lo = target - grace_ms
        hi = target + grace_ms
        best: tuple[float, int] | None = None
        best_key: tuple[int, int] | None = None  # (abs_delta, prefer_after)
        for ts, px in self._active_hist():
            if ts < lo or ts > hi:
                continue
            delta = abs(int(ts) - target)
            after = 0 if int(ts) <= target else 1
            key = (delta, after)
            if best_key is None or key < best_key:
                best_key = key
                best = (float(px), int(ts))
        return best

    def ensure_started(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run(), name="twap-feed")

    async def wait_ready(self, timeout_s: float = 5.0) -> float | None:
        self.ensure_started()
        if self._active_price() is not None:
            return self._active_price()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._active_price() is not None:
                return self._active_price()
            await asyncio.sleep(0.1)
        return self._active_price()

    async def wait_for_chainlink_at_or_after(
        self, window_start_ms: int, *, wait_s: float = 3.0
    ) -> tuple[float, int] | None:
        self.ensure_started()
        hit = self.chainlink_at_or_after(window_start_ms)
        if hit is not None:
            return hit
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            hit = self.chainlink_at_or_after(window_start_ms)
            if hit is not None:
                return hit
        return self.chainlink_at_or_after(window_start_ms)

    async def wait_for_close_twap(
        self, window_end_ms: int, *, wait_s: float = 3.0, grace_ms: int = _BOUNDARY_GRACE_MS
    ) -> tuple[float, int] | None:
        self.ensure_started()
        hit = self.twap_at_close(window_end_ms, grace_ms=grace_ms)
        if hit is not None:
            return hit
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            hit = self.twap_at_close(window_end_ms, grace_ms=grace_ms)
            if hit is not None:
                return hit
        return self.twap_at_close(window_end_ms, grace_ms=grace_ms)

    async def wait_for_open_twap(
        self, window_start_ms: int, *, wait_s: float = 3.0, grace_ms: int = _BOUNDARY_GRACE_MS
    ) -> tuple[float, int] | None:
        self.ensure_started()
        hit = self.twap_at_open(window_start_ms, grace_ms=grace_ms)
        if hit is not None:
            return hit
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            hit = self.twap_at_open(window_start_ms, grace_ms=grace_ms)
            if hit is not None:
                return hit
        return self.twap_at_open(window_start_ms, grace_ms=grace_ms)

    async def resolve_twap_at(
        self, at_ms: int, *, wait_s: float = 3.0, grace_ms: int = _BOUNDARY_GRACE_MS
    ) -> tuple[float, int] | None:
        """
        Polymarket RTDS Chainlink TWAP sample closest to at_ms (active lookback).

        Topics: crypto_prices_twap_thirty / crypto_prices_twap_sixty.
        """
        self.ensure_started()
        hit = self.twap_at_close(at_ms, grace_ms=grace_ms)
        if hit is None:
            now = int(time.time() * 1000)
            if abs(now - int(at_ms)) < 20_000 and wait_s > 0:
                deadline = time.monotonic() + wait_s
                while time.monotonic() < deadline and hit is None:
                    await asyncio.sleep(0.15)
                    hit = self.twap_at_close(at_ms, grace_ms=grace_ms)
        return hit

    def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error = str(exc)
            if self._running:
                await asyncio.sleep(2.0)

    async def _session(self) -> None:
        # websockets defaults to proxy=True (system HTTP(S)_PROXY). That is required
        # on some networks, but a broken local proxy yields InvalidProxyMessage —
        # fall back to a direct connect so Current Price can recover.
        last_exc: Exception | None = None
        for proxy in (True, None):
            try:
                async with websockets.connect(
                    RTDS_URL,
                    ping_interval=None,
                    ping_timeout=None,
                    open_timeout=8,
                    max_size=2**20,
                    proxy=proxy,
                ) as ws:
                    await ws.send(json.dumps(_subscribe_payload(self.rtds_symbol)))
                    self._error = None
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
                        self._handle_message(msg)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                self._error = str(exc)
                continue
        if last_exc is not None:
            self._error = str(last_exc)

    def _handle_message(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        topic = str(msg.get("topic") or "")
        if msg.get("statusCode") or msg.get("error") or msg.get("message") == "topic not found":
            self._error = str(msg.get("message") or msg.get("error") or msg)
            return

        payload = msg.get("payload") or {}
        if not isinstance(payload, dict):
            return
        symbol = str(payload.get("symbol") or "").lower()
        if symbol and symbol not in self._allowed_symbols:
            return

        value = payload.get("value")
        # Prefer Chainlink full-accuracy E18 when present (docs: `value` is display-only).
        raw_full = payload.get("full_accuracy_value")
        value_f: float | None = None
        try:
            if value is not None:
                value_f = float(value)
        except (TypeError, ValueError):
            value_f = None
        if raw_full is not None:
            try:
                full_f = float(raw_full) / 1e18
                if value_f is None or abs(full_f - value_f) <= max(1.0, abs(value_f) * 1e-4):
                    value_f = full_f
            except (TypeError, ValueError):
                pass
        price = value_f
        if price is None:
            return

        ts = payload.get("timestamp")
        try:
            ts_ms = int(ts) if ts is not None else int(time.time() * 1000)
        except (TypeError, ValueError):
            ts_ms = int(time.time() * 1000)

        window = payload.get("window_s") or payload.get("windowSeconds") or payload.get(
            "window_seconds"
        )
        window_i: int | None = None
        try:
            if window is not None:
                window_i = int(window)
        except (TypeError, ValueError):
            window_i = None

        if topic == "crypto_prices_twap_thirty" or (
            topic == "prices.crypto.chainlink.twap" and window_i == 30
        ):
            self._twap_30 = price
            self._twap_30_ts = ts_ms
            self._twap_30_hist.append((ts_ms, price))
            self._error = None
            return

        if topic == "crypto_prices_twap_sixty" or (
            topic == "prices.crypto.chainlink.twap" and window_i == 60
        ):
            self._twap_60 = price
            self._twap_60_ts = ts_ms
            self._twap_60_hist.append((ts_ms, price))
            self._error = None
            return

        # Legacy generic TWAP topic without window → treat as 30s.
        if topic == "prices.crypto.chainlink.twap" and window_i is None:
            self._twap_30 = price
            self._twap_30_ts = ts_ms
            self._twap_30_hist.append((ts_ms, price))
            self._error = None
            return

        if topic in {"crypto_prices_chainlink", "prices.crypto.chainlink"}:
            self._chainlink.append((ts_ms, price))
            self._error = None


_TWAP_BY_SYMBOL: dict[str, TwapFeed] = {}


def get_twap_feed(rtds_symbol: str | None = None) -> TwapFeed:
    """Return a shared TwapFeed for the RTDS symbol (default btc/usd)."""
    key = str(rtds_symbol or "btc/usd").strip().lower() or "btc/usd"
    feed = _TWAP_BY_SYMBOL.get(key)
    if feed is None:
        feed = TwapFeed(key)
        _TWAP_BY_SYMBOL[key] = feed
    return feed
