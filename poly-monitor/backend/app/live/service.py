"""Assemble live snapshots for the monitor UI."""

from __future__ import annotations

import json
import re
import time
import asyncio
from typing import Any

from app.core.pricing import quotes_from_up_buy
from app.live.clients import MARKET_DURATION_S, LiveClients, parse_token_ids, window_start_unix
from app.live import ptb_store
from app.live.twap_feed import get_twap_feed

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
        self.twap = get_twap_feed()
        self._market: dict[str, Any] | None = None
        self._market_id: str | None = None
        self._token_up: str | None = None
        self._token_down: str | None = None
        self._window_start_ms: int | None = None
        self._window_end_ms: int | None = None
        self._price_to_beat: float | None = None
        self._price_to_beat_source: str | None = None
        self._last_discover_s = 0.0
        # Start RTDS early so we can capture the current market's open TWAP.
        self.twap.ensure_started()

    async def close(self) -> None:
        self.twap.stop()
        await self.clients.close()

    def _with_twap(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.twap.ensure_started()
        payload.update(self.twap.latest())
        return payload

    def _persist_open_twap_for_next(self, next_start_ms: int) -> None:
        """
        At a window boundary, save the open 30s TWAP for the market that starts
        at next_start_ms (same instant as the prior market's end).
        """
        start = int(next_start_ms)
        hit = self.twap.twap_at_open(start)
        if hit is None and self.twap.price is not None and self.twap.timestamp_ms is not None:
            if abs(int(self.twap.timestamp_ms) - start) <= 5_000:
                hit = (float(self.twap.price), int(self.twap.timestamp_ms))
        if hit is None:
            return
        price, obs_ts = hit
        ptb_store.set_price_to_beat(
            start, price, source="open_twap_30s", observed_ts=obs_ts
        )

    async def _maybe_capture_open_twap(self) -> None:
        """Near / just after a window boundary, lock open TWAP for the new market."""
        if self._window_end_ms is None:
            return
        # Next market opens when this one ends.
        next_start = int(self._window_end_ms)
        now_ms = int(time.time() * 1000)
        if now_ms < next_start - 3_000 or now_ms > next_start + 15_000:
            return
        if ptb_store.get_price_to_beat(next_start) is not None:
            return
        self.twap.ensure_started()
        hit = self.twap.twap_at_open(next_start)
        if hit is None and now_ms <= next_start + 5_000:
            hit = await self.twap.wait_for_open_twap(next_start, timeout_s=2.0)
        if hit is not None:
            price, obs_ts = hit
            ptb_store.set_price_to_beat(
                next_start, price, source="open_twap_30s", observed_ts=obs_ts
            )
            return
        if now_ms >= next_start:
            computed = await self.clients.compute_twap_30s_ending_at(next_start)
            if computed is not None:
                ptb_store.set_price_to_beat(
                    next_start,
                    computed,
                    source="open_twap_30s_computed",
                    observed_ts=next_start,
                )

    async def _resolve_price_to_beat(self, window_start_ms: int) -> None:
        """
        Price To Beat = current market's open Chainlink 30s TWAP (at window start).

        Not live BTC spot, and not the TWAP value at page-load time.

        Resolution order:
          1) persisted open TWAP for this window
          2) RTDS 30s TWAP sample nearest to / first at market open
          3) briefly wait if we are still near open
          4) compute 30s TWAP from Binance over [T0−30s, T0]
        """
        if self._price_to_beat is not None:
            return
        start = int(window_start_ms)
        now_ms = int(time.time() * 1000)

        stored = ptb_store.get_price_to_beat(start)
        if stored is not None:
            self._price_to_beat = float(stored["price"])
            self._price_to_beat_source = str(stored.get("source") or "open_twap_30s")
            return

        self.twap.ensure_started()
        hit = self.twap.twap_at_open(start)
        if hit is None and now_ms - start < 25_000:
            hit = await self.twap.wait_for_open_twap(start, timeout_s=4.0)

        if hit is not None:
            price, obs_ts = hit
            self._price_to_beat = float(price)
            self._price_to_beat_source = "open_twap_30s"
            ptb_store.set_price_to_beat(
                start, price, source="open_twap_30s", observed_ts=obs_ts
            )
            return

        # Missed RTDS open sample: reconstruct open TWAP exactly.
        computed = await self.clients.compute_twap_30s_ending_at(start)
        if computed is not None:
            self._price_to_beat = float(computed)
            self._price_to_beat_source = "open_twap_30s_computed"
            ptb_store.set_price_to_beat(
                start, computed, source="open_twap_30s_computed", observed_ts=start
            )

    async def _lock_price_to_beat(self, *, btc: float | None) -> None:
        # Kept for call sites; ignores live `btc` so reload cannot overwrite PTB.
        del btc
        await self._maybe_capture_open_twap()
        if self._window_start_ms is None:
            return
        await self._resolve_price_to_beat(self._window_start_ms)

    async def _ensure_market(self, *, force: bool = False) -> dict[str, Any] | None:
        now = time.time()
        wall_window_ms = window_start_unix(now) * 1000
        need = force or self._market is None or (now - self._last_discover_s) > 15
        if self._window_end_ms is not None and now * 1000 >= self._window_end_ms:
            need = True
        # New 5m slot on the wall clock → force rediscovery even if Gamma lags.
        if self._window_start_ms is not None and wall_window_ms != self._window_start_ms:
            need = True
        if not need:
            return self._market

        market = await self.clients.discover_active_updown()
        self._last_discover_s = now
        if not market:
            # Wall clock moved on; clear beat for the new slot until Gamma catches up.
            if self._window_start_ms is not None and wall_window_ms != self._window_start_ms:
                self._market = None
                self._market_id = None
                self._token_up = None
                self._token_down = None
                self._window_start_ms = wall_window_ms
                self._window_end_ms = wall_window_ms + MARKET_DURATION_S * 1000
                self._price_to_beat = None
            else:
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
        start_ms = start_s * 1000

        rolled = market_id != self._market_id or start_ms != self._window_start_ms
        if rolled and self._window_end_ms is not None:
            # Boundary TWAP becomes the next market's open Price To Beat.
            self._persist_open_twap_for_next(self._window_end_ms)
        self._market = market
        self._market_id = market_id
        self._token_up = token_up
        self._token_down = token_down
        self._window_start_ms = start_ms
        self._window_end_ms = end_s * 1000
        if rolled:
            self._price_to_beat = None
            self._price_to_beat_source = None
            # Prefer persisted open TWAP for this window immediately.
            stored = ptb_store.get_price_to_beat(start_ms)
            if stored is not None:
                self._price_to_beat = float(stored["price"])
                self._price_to_beat_source = str(
                    stored.get("source") or "open_twap_30s"
                )
        return market

    async def snapshot(self) -> dict[str, Any]:
        market = await self._ensure_market()
        now_ms = int(time.time() * 1000)
        self.twap.ensure_started()

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
            return self._with_twap(
                {
                    "type": "error",
                    "message": f"BTC price unavailable: {exc}",
                    "timestamp": now_ms,
                }
            )

        await self._lock_price_to_beat(btc=btc)

        if market is None or not self._token_up:
            return self._with_twap(
                {
                    "type": "tick",
                    "live": True,
                    "timestamp": now_ms,
                    "market_id": None,
                    "slug": None,
                    "start_time": self._window_start_ms,
                    "end_time": self._window_end_ms,
                    "btc_price": btc,
                    "price_to_beat": self._price_to_beat,
                    "price_to_beat_source": self._price_to_beat_source,
                    "btc_open": self._price_to_beat,
                    "up_price": 0.5,
                    "down_price": 0.5,
                    "up_buy": 0.5,
                    "down_buy": 0.5,
                    "up_sell": 0.49,
                    "down_sell": 0.49,
                    "remaining_seconds": max(
                        0.0,
                        ((self._window_end_ms or now_ms) - now_ms) / 1000.0,
                    ),
                    "elapsed_seconds": max(
                        0.0,
                        (now_ms - (self._window_start_ms or now_ms)) / 1000.0,
                    ),
                    "book": {
                        "timestamp": now_ms,
                        "mode": "ladder",
                        "note": "No active btc-updown-5m market found",
                        "up": None,
                        "down": None,
                    },
                    "error": "No active market",
                }
            )

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

        return self._with_twap(
            {
                "type": "tick",
                "live": True,
                "timestamp": now_ms,
                "market_id": self._market_id,
                "slug": str(market.get("slug") or ""),
                "start_time": start_ms,
                "end_time": end_ms,
                "btc_price": btc,
                "price_to_beat": self._price_to_beat,
                "price_to_beat_source": self._price_to_beat_source,
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
        )

    async def market_meta(self) -> dict[str, Any] | None:
        self.twap.ensure_started()
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
            "price_to_beat": self._price_to_beat,
            "price_to_beat_source": self._price_to_beat_source,
            **self.twap.latest(),
        }


_LIVE: LiveMarketService | None = None


def get_live_service() -> LiveMarketService:
    global _LIVE
    if _LIVE is None:
        _LIVE = LiveMarketService()
    return _LIVE
