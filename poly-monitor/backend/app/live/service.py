"""Assemble live snapshots for the monitor UI."""

from __future__ import annotations

import json
import re
import time
import asyncio
from typing import Any

from app.core.pricing import quotes_from_up_buy
from app.live.clients import MARKET_DURATION_S, LiveClients, parse_token_ids, window_start_unix

_UPDOWN_SLUG_RE = re.compile(r"(?i)^btc-updown-5m-(\d+)$")


def _levels_from_book(raw: dict[str, Any], *, side: str, limit: int = 12) -> list[dict[str, Any]]:
    """Normalize CLOB asks/bids into ladder rows (price, shares, notional)."""
    key = "asks" if side == "ask" else "bids"
    rows = raw.get(key) or []
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            price = float(row.get("price"))
            shares = float(row.get("size") or row.get("shares") or 0)
        except (TypeError, ValueError):
            continue
        if shares <= 0:
            continue
        out.append(
            {
                "price": price,
                "shares": shares,
                "approx_price": price,
                "notional": price * shares,
                "range": f"{round(price * 100)}¢",
                "suffix": f"{price:.4f}",
                "price_lo": price,
                "price_hi": price,
            }
        )
    if side == "ask":
        out.sort(key=lambda x: x["price"])
        out = list(reversed(out[:limit]))
    else:
        out.sort(key=lambda x: x["price"], reverse=True)
        out = out[:limit]
    return out


def _best_bid_ask(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    bids = raw.get("bids") or []
    asks = raw.get("asks") or []
    best_bid = None
    best_ask = None
    try:
        if bids:
            best_bid = max(float(b["price"]) for b in bids if b.get("price") is not None)
    except (TypeError, ValueError, KeyError):
        best_bid = None
    try:
        if asks:
            best_ask = min(float(a["price"]) for a in asks if a.get("price") is not None)
    except (TypeError, ValueError, KeyError):
        best_ask = None
    return best_bid, best_ask


def _up_buy_from_book(raw: dict[str, Any]) -> float | None:
    best_bid, best_ask = _best_bid_ask(raw)
    if best_ask is not None:
        return best_ask
    if best_bid is not None:
        return best_bid
    return None


def _side_book(raw: dict[str, Any], traded: float) -> dict[str, Any]:
    best_bid, best_ask = _best_bid_ask(raw)
    spread = None
    if best_bid is not None and best_ask is not None:
        spread = max(0.0, best_ask - best_bid)
    asks = _levels_from_book(raw, side="ask")
    bids = _levels_from_book(raw, side="bid")
    ask_total = sum(x["shares"] for x in asks)
    bid_total = sum(x["shares"] for x in bids)
    return {
        "traded_price": traded,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "asks": asks,
        "bids": bids,
        "ask_shares": ask_total,
        "bid_shares": bid_total,
        "volume_shares": ask_total + bid_total,
    }


class LiveMarketService:
    def __init__(self) -> None:
        self.clients = LiveClients()
        self._market: dict[str, Any] | None = None
        self._market_id: str | None = None
        self._token_up: str | None = None
        self._token_down: str | None = None
        self._window_start_ms: int | None = None
        self._window_end_ms: int | None = None
        self._price_to_beat: float | None = None
        self._last_discover_s = 0.0

    async def close(self) -> None:
        await self.clients.close()

    async def _ensure_market(self, *, force: bool = False) -> dict[str, Any] | None:
        now = time.time()
        need = force or self._market is None or (now - self._last_discover_s) > 20
        if self._window_end_ms is not None and now * 1000 >= self._window_end_ms:
            need = True
        if not need:
            return self._market

        market = await self.clients.discover_active_updown()
        self._last_discover_s = now
        if not market:
            self._market = None
            return None

        market_id = str(market.get("id") or market.get("conditionId") or "")
        slug = str(market.get("slug") or "")
        token_up, token_down = parse_token_ids(market)

        match = _UPDOWN_SLUG_RE.match(slug)
        if match:
            start_s = int(match.group(1))
        else:
            start_s = window_start_unix(now)
        end_s = start_s + MARKET_DURATION_S

        rolled = market_id != self._market_id
        self._market = market
        self._market_id = market_id
        self._token_up = token_up
        self._token_down = token_down
        self._window_start_ms = start_s * 1000
        self._window_end_ms = end_s * 1000
        if rolled:
            self._price_to_beat = None
        return market

    async def snapshot(self) -> dict[str, Any]:
        market = await self._ensure_market()
        now_ms = int(time.time() * 1000)

        async def _btc() -> float:
            return await self.clients.get_btc_price()

        async def _book(token: str | None) -> dict[str, Any]:
            if not token:
                return {"bids": [], "asks": []}
            try:
                return await self.clients.get_order_book(token)
            except Exception:
                return {"bids": [], "asks": []}

        try:
            if market is None or not self._token_up:
                btc = await _btc()
            else:
                btc, up_book, down_book = await asyncio.gather(
                    _btc(),
                    _book(self._token_up),
                    _book(self._token_down),
                )
        except Exception as exc:
            return {
                "type": "error",
                "message": f"BTC price unavailable: {exc}",
                "timestamp": now_ms,
            }

        if market is None or not self._token_up:
            return {
                "type": "tick",
                "live": True,
                "timestamp": now_ms,
                "market_id": None,
                "slug": None,
                "start_time": None,
                "end_time": None,
                "btc_price": btc,
                "price_to_beat": self._price_to_beat,
                "btc_open": self._price_to_beat,
                "up_price": 0.5,
                "down_price": 0.5,
                "up_buy": 0.5,
                "down_buy": 0.5,
                "up_sell": 0.49,
                "down_sell": 0.49,
                "remaining_seconds": 0,
                "elapsed_seconds": 0,
                "book": {
                    "timestamp": now_ms,
                    "mode": "ladder",
                    "note": "No active btc-updown-5m market found",
                    "up": None,
                    "down": None,
                },
                "error": "No active market",
            }

        if self._price_to_beat is None:
            self._price_to_beat = btc

        up_buy = _up_buy_from_book(up_book)
        if up_buy is None:
            try:
                raw_prices = market.get("outcomePrices") or market.get("outcome_prices")
                if isinstance(raw_prices, str):
                    raw_prices = json.loads(raw_prices)
                if raw_prices:
                    up_buy = float(raw_prices[0])
            except Exception:
                up_buy = None
        q = quotes_from_up_buy(up_buy)

        start_ms = self._window_start_ms or now_ms
        end_ms = self._window_end_ms or (now_ms + 1)
        remaining = max(0.0, (end_ms - now_ms) / 1000.0)
        elapsed = max(0.0, (now_ms - start_ms) / 1000.0)

        book = {
            "timestamp": now_ms,
            "mode": "ladder",
            "note": "Live CLOB ladder (top levels). Down buy derived as 101¢ − Up buy.",
            "up": _side_book(up_book, q["up_price"]),
            "down": _side_book(down_book, q["down_price"]) if down_book else None,
            "up_price": q["up_price"],
            "down_price": q["down_price"],
            "up_buy": q["up_buy"],
            "down_buy": q["down_buy"],
            "up_sell": q["up_sell"],
            "down_sell": q["down_sell"],
        }

        return {
            "type": "tick",
            "live": True,
            "timestamp": now_ms,
            "market_id": self._market_id,
            "slug": str(market.get("slug") or ""),
            "start_time": start_ms,
            "end_time": end_ms,
            "btc_price": btc,
            "price_to_beat": self._price_to_beat,
            "btc_open": self._price_to_beat,
            "up_price": q["up_price"],
            "down_price": q["down_price"],
            "up_buy": q["up_buy"],
            "down_buy": q["down_buy"],
            "up_sell": q["up_sell"],
            "down_sell": q["down_sell"],
            "remaining_seconds": remaining,
            "elapsed_seconds": elapsed,
            "book": book,
        }

    async def market_meta(self) -> dict[str, Any] | None:
        market = await self._ensure_market(force=True)
        if not market:
            return None
        return {
            "type": "market",
            "live": True,
            "market_id": self._market_id,
            "slug": str(market.get("slug") or ""),
            "start_time": self._window_start_ms,
            "end_time": self._window_end_ms,
            "token_up": self._token_up,
            "token_down": self._token_down,
        }


_LIVE: LiveMarketService | None = None


def get_live_service() -> LiveMarketService:
    global _LIVE
    if _LIVE is None:
        _LIVE = LiveMarketService()
    return _LIVE
