"""Polymarket RTDS activity/trades — live fills for sidebar + volume bars."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any

import httpx
import websockets

logger = logging.getLogger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
SUBSCRIBE = {
    "action": "subscribe",
    "subscriptions": [{"topic": "activity", "type": "trades"}],
}


class ActivityFeed:
    """Background RTDS subscriber for market activity trades."""

    def __init__(self, *, maxlen: int = 200) -> None:
        self._pending: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seen: deque[str] = deque(maxlen=2_000)
        self._seen_set: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._resub = asyncio.Event()
        self._slug: str | None = None
        self._condition_id: str | None = None
        self._token_up: str | None = None
        self._token_down: str | None = None
        self._start_ms: int | None = None
        self._end_ms: int | None = None
        self._error: str | None = None

    def ensure_started(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="rtds-activity")

    def stop(self) -> None:
        self._running = False
        self._resub.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def set_market(
        self,
        *,
        slug: str | None,
        condition_id: str | None,
        token_up: str | None,
        token_down: str | None,
        start_ms: int | None,
        end_ms: int | None,
        clear: bool = False,
    ) -> None:
        slug_n = (slug or "").strip() or None
        cid_n = (condition_id or "").strip() or None
        start_n = int(start_ms) if start_ms else None
        end_n = int(end_ms) if end_ms else None
        changed = (
            slug_n != self._slug
            or cid_n != self._condition_id
            or token_up != self._token_up
            or token_down != self._token_down
            or start_n != self._start_ms
            or end_n != self._end_ms
        )
        self._slug = slug_n
        self._condition_id = cid_n
        self._token_up = token_up
        self._token_down = token_down
        self._start_ms = start_n
        self._end_ms = end_n
        if clear:
            self._pending.clear()
            self._seen.clear()
            self._seen_set.clear()
        # Only bounce the socket when the market filter actually changes.
        if changed:
            self._resub.set()
        self.ensure_started()
        if changed and cid_n:
            # Best-effort seed so the tape isn't empty while RTDS catches up.
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.seed_from_data_api(limit=40))
            except RuntimeError:
                pass

    def drain(self, *, limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while self._pending and len(out) < limit:
            out.append(self._pending.popleft())
        return out

    async def seed_from_data_api(self, *, limit: int = 40) -> None:
        cid = self._condition_id
        if not cid:
            return
        try:
            async with httpx.AsyncClient(
                base_url=DATA_API_URL,
                timeout=httpx.Timeout(12.0, connect=5.0),
            ) as http:
                resp = await http.get(
                    "/trades",
                    params={"market": cid, "limit": min(100, max(1, limit)), "offset": 0},
                )
                resp.raise_for_status()
                raw = resp.json()
        except Exception as exc:
            logger.debug("activity data-api seed failed: %s", exc)
            return
        rows = raw if isinstance(raw, list) else []
        # API is newest-first; emit oldest-first so UI sorts cleanly.
        for item in reversed(rows):
            if isinstance(item, dict):
                self._emit(item)

    async def _run(self) -> None:
        while self._running:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error = str(exc)
            if self._running:
                await asyncio.sleep(1.5)

    async def _session(self) -> None:
        self._resub.clear()
        async with websockets.connect(
            RTDS_URL,
            ping_interval=None,
            ping_timeout=None,
            max_size=2**22,
        ) as ws:
            await ws.send(json.dumps(SUBSCRIBE))
            self._error = None
            ping_at = time.monotonic()
            while self._running:
                if self._resub.is_set():
                    self._resub.clear()
                    return
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
        if str(msg.get("topic") or "") != "activity":
            return
        if str(msg.get("type") or "") not in {"trades", "trade"}:
            return
        payload = msg.get("payload")
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    self._emit(item)
            return
        if isinstance(payload, dict):
            self._emit(payload)

    def _emit(self, trade: dict[str, Any]) -> None:
        # Match by conditionId, token asset, or slug (any one is enough).
        if self._condition_id or self._token_up or self._token_down or self._slug:
            cid = str(trade.get("conditionId") or trade.get("condition_id") or "")
            asset = str(trade.get("asset") or trade.get("asset_id") or "")
            slug = str(
                trade.get("slug")
                or trade.get("market_slug")
                or trade.get("eventSlug")
                or trade.get("event_slug")
                or ""
            )
            ok = False
            if self._condition_id and cid and cid.lower() == self._condition_id.lower():
                ok = True
            if self._token_up and asset and asset == str(self._token_up):
                ok = True
            if self._token_down and asset and asset == str(self._token_down):
                ok = True
            if self._slug and slug and (
                slug == self._slug or self._slug in slug or slug in self._slug
            ):
                ok = True
            if not ok:
                return

        row = self._to_row(trade)
        if row is None:
            return
        key = str(row.get("id") or "")
        if key:
            if key in self._seen_set:
                return
            self._seen_set.add(key)
            self._seen.append(key)
            while len(self._seen) > 1_800 and self._seen:
                old = self._seen.popleft()
                self._seen_set.discard(old)
        self._pending.append(row)

    def _to_row(self, trade: dict[str, Any]) -> dict[str, Any] | None:
        tx = str(trade.get("transactionHash") or trade.get("transaction_hash") or "")
        try:
            ts = int(trade.get("timestamp") or 0)
        except (TypeError, ValueError):
            return None
        if ts <= 0:
            return None
        if ts < 10_000_000_000:
            ts *= 1000
        start_ms = self._start_ms or 0
        end_ms = self._end_ms or 0
        if end_ms and ts >= end_ms + 60_000:
            return None
        if start_ms and ts < start_ms - 120_000:
            return None

        asset = str(trade.get("asset") or trade.get("asset_id") or "")
        is_down: bool | None = None
        if self._token_up and asset == str(self._token_up):
            is_down = False
        elif self._token_down and asset == str(self._token_down):
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
        is_sell = side_raw in {"SELL", "S"}
        try:
            price = float(trade.get("price") or 0)
            size = float(trade.get("size") or 0)
        except (TypeError, ValueError):
            return None
        if size <= 0 or price < 0:
            return None

        wallet = str(
            trade.get("proxyWallet")
            or trade.get("proxy_wallet")
            or trade.get("wallet")
            or ""
        )
        name = str(trade.get("name") or trade.get("pseudonym") or "").strip()
        if not name and wallet:
            name = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet
        if not name:
            name = "Trader"
        shares = float(size)
        return {
            "id": tx or f"{ts}:{wallet}:{asset}:{price}:{size}",
            "timestamp": ts,
            "name": name,
            "pseudonym": str(trade.get("pseudonym") or "") or None,
            "proxy_wallet": wallet,
            "profile_image": str(trade.get("profileImage") or trade.get("profile_image") or "")
            or None,
            "outcome": "Down" if is_down else "Up",
            "side": "SELL" if is_sell else "BUY",
            "price": price,
            "shares": shares,
            "usd": round(price * shares, 2),
            "transaction_hash": tx or None,
            # Volume chart keys (token False=UP, side False=BUY)
            "token": bool(is_down),
            "is_sell": bool(is_sell),
        }


_FEED: ActivityFeed | None = None


def get_activity_feed() -> ActivityFeed:
    global _FEED
    if _FEED is None:
        _FEED = ActivityFeed()
    return _FEED
