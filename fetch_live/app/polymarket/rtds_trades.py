"""Polymarket RTDS activity/trades — real-time fills with proxyWallet."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

import websockets
from loguru import logger

from app.config import settings

OnTrade = Callable[[dict[str, Any]], None]


class RtdsTrades:
    """Subscribe to RTDS topic=activity type=trades (includes proxyWallet)."""

    def __init__(self, *, on_trade: OnTrade | None = None) -> None:
        self.on_trade = on_trade
        self._running = False
        self._slug: str | None = None
        self._condition_id: str | None = None
        self._token_up: str | None = None
        self._token_down: str | None = None
        self._start_ms: int | None = None
        self._end_ms: int | None = None
        self._resub = asyncio.Event()

    def set_market(
        self,
        *,
        slug: str | None,
        condition_id: str | None,
        token_up: str | None,
        token_down: str | None,
        start_ms: int,
        end_ms: int,
    ) -> None:
        self._slug = (slug or "").strip() or None
        self._condition_id = (condition_id or "").strip() or None
        self._token_up = token_up
        self._token_down = token_down
        self._start_ms = int(start_ms)
        self._end_ms = int(end_ms)
        self._resub.set()

    def stop(self) -> None:
        self._running = False
        self._resub.set()

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("RTDS trades error: {}", exc)
            if self._running:
                await asyncio.sleep(1.5)

    def _subscribe_payload(self) -> dict[str, Any]:
        # Unfiltered activity stream; filter client-side by conditionId / slug.
        # Server-side market_slug filters are unreliable for up/down markets.
        return {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "activity", "type": "trades"},
            ],
        }

    async def _session(self) -> None:
        self._resub.clear()
        async with websockets.connect(
            settings.rtds_url,
            ping_interval=None,
            ping_timeout=None,
            max_size=2**22,
        ) as ws:
            await ws.send(json.dumps(self._subscribe_payload()))
            logger.info(
                "RTDS trades subscribed slug={} cid={}",
                self._slug,
                (self._condition_id or "")[:18] or None,
            )
            ping_at = time.monotonic()
            while self._running:
                if self._resub.is_set():
                    self._resub.clear()
                    # Reconnect so server-side filter matches new market.
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
        topic = str(msg.get("topic") or "")
        mtype = str(msg.get("type") or "")
        if topic != "activity" or mtype not in {"trades", "trade"}:
            return
        payload = msg.get("payload")
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    self._emit_payload(item)
            return
        if isinstance(payload, dict):
            self._emit_payload(payload)

    def _emit_payload(self, trade: dict[str, Any]) -> None:
        if not self.on_trade:
            return
        if self._condition_id:
            cid = str(trade.get("conditionId") or trade.get("condition_id") or "")
            if cid and cid.lower() != self._condition_id.lower():
                return
        if self._slug:
            slug = str(
                trade.get("slug")
                or trade.get("market_slug")
                or trade.get("eventSlug")
                or trade.get("event_slug")
                or ""
            )
            if slug and slug != self._slug:
                return

        row = self._to_row(trade)
        if row is not None:
            self.on_trade(row)

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
        if end_ms and ts >= end_ms:
            return None
        if start_ms and ts < start_ms - 300_000:
            return None

        asset = str(trade.get("asset") or trade.get("asset_id") or "")
        is_up: bool | None = None
        if self._token_up and asset == str(self._token_up):
            is_up = True
        elif self._token_down and asset == str(self._token_down):
            is_up = False
        else:
            outcome = str(trade.get("outcome") or "").strip().lower()
            if outcome in {"up", "yes"}:
                is_up = True
            elif outcome in {"down", "no"}:
                is_up = False
            else:
                try:
                    is_up = int(trade.get("outcomeIndex")) == 0
                except (TypeError, ValueError):
                    return None
        if is_up is None:
            return None

        side_raw = str(trade.get("side") or "BUY").upper()
        is_buy = side_raw not in {"SELL", "S"}
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
        ).strip().lower()
        # RTDS activity stream is the aggressive / visible fill → taker.
        return {
            "timestamp": ts,
            "transaction_hash": tx,
            "wallet": wallet,
            "is_up": bool(is_up),
            "is_buy": bool(is_buy),
            "is_taker": True,
            "price": price,
            "shares": round(max(0.0, float(size)), 2),
        }
