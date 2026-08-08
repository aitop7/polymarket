"""Assemble live snapshots for the monitor UI."""

from __future__ import annotations

import json
import re
import time
import asyncio
from collections import deque
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.pricing import quotes_from_up_buy
from app.live.clients import MARKET_DURATION_S, LiveClients, parse_token_ids, window_start_unix
from app.live import ptb_store
from app.live.fetch_live_series import (
    break_outcome_jumps,
    load_fetch_live_series,
    merge_series,
    scrub_leading_outcome_extremes,
)
from app.live.twap_feed import get_twap_feed
from app.live.vps_sync import get_vps_sync

_UPDOWN_SLUG_RE = re.compile(r"(?i)^btc-updown-5m-(\d+)$")

# Don't lock PTB before the open boundary; refine while near open.
_PTB_REFINE_MS = 15_000
_SERIES_MAX = 900


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
        self._condition_id: str | None = None
        self._token_up: str | None = None
        self._token_down: str | None = None
        self._window_start_ms: int | None = None
        self._window_end_ms: int | None = None
        self._price_to_beat: float | None = None
        self._price_to_beat_source: str | None = None
        self._fetch_live_open_px: float | None = None
        self._fetch_live_open_for: int | None = None
        self._fetch_live_open_obs: int | None = None
        self._last_discover_s = 0.0
        self._series: deque[dict[str, Any]] = deque(maxlen=_SERIES_MAX)
        self._series_market_id: str | None = None
        self._holders_cache: dict[str, Any] | None = None
        self._holders_cache_at = 0.0
        # Start RTDS early so open TWAP (Price to Beat) is ready at market open.
        self.twap.ensure_started()

    async def close(self) -> None:
        self.twap.stop()
        await self.clients.close()

    def _with_twap(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.twap.ensure_started()
        payload.update(self.twap.latest())
        return payload

    def _lookup_fetch_live_open(
        self, *, market_id: str | None, window_start_ms: int
    ) -> tuple[float, int | None] | None:
        """Nearest RTDS TWAP from fetch_live parquet; else meta.json open."""
        start = int(window_start_ms)
        if self._fetch_live_open_for == start and self._fetch_live_open_px is not None:
            return self._fetch_live_open_px, self._fetch_live_open_obs

        root = Path(settings.fetch_live_data_dir)
        if not root.is_dir():
            return None

        market_dirs: list[Path] = []
        if market_id:
            for day in sorted(root.iterdir(), reverse=True)[:4]:
                if day.is_dir():
                    d = day / str(market_id)
                    if d.is_dir():
                        market_dirs.append(d)
                        break
        if not market_dirs:
            for day in sorted(root.iterdir(), reverse=True)[:2]:
                if not day.is_dir():
                    continue
                for market_dir in day.iterdir():
                    if market_dir.is_dir():
                        market_dirs.append(market_dir)

        for market_dir in market_dirs:
            meta_path = market_dir / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                row = json.loads(meta_path.read_text(encoding="utf-8"))
                if int(row.get("start_time") or 0) != start:
                    continue
            except Exception:
                continue

            candidates = [market_dir / "chainlink_price.parquet"]
            try:
                for day in sorted(root.iterdir(), reverse=True)[:2]:
                    if not day.is_dir():
                        continue
                    for other in day.iterdir():
                        if not other.is_dir() or other == market_dir:
                            continue
                        om = other / "meta.json"
                        if not om.is_file():
                            continue
                        try:
                            ometa = json.loads(om.read_text(encoding="utf-8"))
                        except Exception:
                            continue
                        if int(ometa.get("end_time") or 0) == start:
                            candidates.append(other / "chainlink_price.parquet")
                            break
            except Exception:
                pass

            best_px: float | None = None
            best_obs: int | None = None
            best_key: tuple[int, int] | None = None
            for path in candidates:
                if not path.is_file():
                    continue
                try:
                    import pandas as pd

                    df = pd.read_parquet(path, columns=["timestamp", "twap"])
                except Exception:
                    continue
                if df is None or df.empty:
                    continue
                for ts, tw in zip(df["timestamp"].tolist(), df["twap"].tolist()):
                    try:
                        tsi = int(ts)
                        px = float(tw)
                    except (TypeError, ValueError):
                        continue
                    if abs(tsi - start) > 5_000:
                        continue
                    delta = abs(tsi - start)
                    after = 0 if tsi <= start else 1
                    key = (delta, after)
                    if best_key is None or key < best_key:
                        best_key = key
                        best_px = px
                        best_obs = tsi

            if best_px is not None:
                self._fetch_live_open_for = start
                self._fetch_live_open_px = best_px
                self._fetch_live_open_obs = best_obs
                return best_px, best_obs

            open_px = row.get("btc_open_price")
            if open_px is None:
                continue
            try:
                px = float(open_px)
            except (TypeError, ValueError):
                continue
            self._fetch_live_open_for = start
            self._fetch_live_open_px = px
            self._fetch_live_open_obs = start
            return px, start
        return None

    def _apply_ptb(self, price: float, source: str, observed_ts: int | None) -> None:
        self._price_to_beat = float(price)
        self._price_to_beat_source = source
        if self._window_start_ms is not None:
            ptb_store.set_price_to_beat(
                self._window_start_ms,
                price,
                source=source,
                observed_ts=observed_ts,
                overwrite=(source == "fetch_live_meta"),
            )

    def _persist_open_twap(self, window_start_ms: int) -> None:
        """
        Persist RTDS sample only if it is already close to T0.
        Early/pre-open samples must not hard-lock Price To Beat.
        """
        start = int(window_start_ms)
        hit = self.twap.twap_at_close(start)
        if hit is None:
            return
        price, obs_ts = hit
        if not ptb_store.is_good_sample(start, obs_ts):
            return
        ptb_store.set_price_to_beat(
            start, price, source="open_twap_30s", observed_ts=obs_ts
        )

    async def _fetch_open_price(
        self, window_start_ms: int, *, wait_s: float = 3.0
    ) -> tuple[float, str, int] | None:
        """
        Price To Beat / btc_open = Polymarket Chainlink 30s TWAP at start_time.

        Primary: RTDS wss://ws-live-data.polymarket.com
                 topic crypto_prices_twap_thirty, filter btc/usd
                 sample closest to market start_time (prefer at/before T0)
        Fallback: Binance REST aggTrades BTCUSDT TWAP over [start−30s, start]
        """
        start_ms = int(window_start_ms)
        hit = await self.twap.resolve_twap_at(start_ms, wait_s=wait_s)
        if hit is not None:
            price, obs_ts = hit
            return float(price), "open_twap_30s", int(obs_ts)

        computed = await self.clients.compute_twap_30s_ending_at(start_ms)
        if computed is not None:
            return float(computed), "open_twap_30s_computed", start_ms
        return None

    async def _maybe_capture_open_twap_for_next(self) -> None:
        """After current window end, capture open TWAP for the next market."""
        if self._window_end_ms is None:
            return
        # Next market opens when this one closes (T1 → next T0).
        start = int(self._window_end_ms)
        now_ms = int(time.time() * 1000)
        # Never lock before the open boundary — that caused early wrong PTBs.
        if now_ms < start or now_ms > start + _PTB_REFINE_MS:
            return
        stored = ptb_store.get_price_to_beat(start)
        if stored is not None and ptb_store.is_good_sample(start, stored.get("observed_ts")):
            return
        wait_s = 3.0 if now_ms <= start + 5_000 else 0.5
        fetched = await self._fetch_open_price(start, wait_s=wait_s)
        if fetched is None:
            return
        price, source, obs_ts = fetched
        ptb_store.set_price_to_beat(
            start, price, source=source, observed_ts=obs_ts
        )

    async def _resolve_price_to_beat(self, window_start_ms: int) -> None:
        """
        Price To Beat = Chainlink 30s TWAP nearest window start (same as Polymarket).

        Prefer live RTDS samples. fetch_live meta is a fallback only — never blocks
        a closer RTDS lock (meta often uses a provisional/Binance value).
        """
        start_ms = int(window_start_ms)
        now_ms = int(time.time() * 1000)

        stored = ptb_store.get_price_to_beat(start_ms)
        good = (
            stored is not None
            and ptb_store.is_good_sample(start_ms, stored.get("observed_ts"))
            and str(stored.get("source") or "") != "fetch_live_meta"
        )
        refining = 0 <= (now_ms - start_ms) <= _PTB_REFINE_MS
        meta_only = (
            stored is not None and str(stored.get("source") or "") == "fetch_live_meta"
        )

        if stored is not None and good and not refining and not meta_only:
            self._price_to_beat = float(stored["price"])
            self._price_to_beat_source = str(
                stored.get("source") or "open_twap_30s"
            )
            return

        if now_ms < start_ms:
            # Not open yet — surface provisional values but do not hard-lock early.
            meta_hit = self._lookup_fetch_live_open(
                market_id=self._market_id, window_start_ms=start_ms
            )
            if meta_hit is not None:
                px, obs = meta_hit
                self._price_to_beat = float(px)
                self._price_to_beat_source = (
                    "open_twap_30s" if obs is not None and obs != start_ms else "fetch_live_meta"
                )
            elif stored is not None:
                self._price_to_beat = float(stored["price"])
                self._price_to_beat_source = str(
                    stored.get("source") or "open_twap_30s"
                )
            return

        wait_s = 4.0 if now_ms - start_ms < 8_000 else (1.0 if refining or meta_only else 0.0)
        fetched = await self._fetch_open_price(start_ms, wait_s=wait_s)
        if fetched is not None:
            price, source, obs_ts = fetched
            wrote = ptb_store.set_price_to_beat(
                start_ms, price, source=source, observed_ts=obs_ts
            )
            if wrote or self._price_to_beat is None or meta_only:
                self._price_to_beat = price
                self._price_to_beat_source = source
                return

        # Fallbacks when RTDS missed the open boundary.
        meta_hit = self._lookup_fetch_live_open(
            market_id=self._market_id, window_start_ms=start_ms
        )
        if meta_hit is not None and (
            self._price_to_beat is None
            or meta_only
            or not good
        ):
            px, obs = meta_hit
            source = (
                "open_twap_30s"
                if obs is not None and ptb_store.is_good_sample(start_ms, obs)
                else "fetch_live_meta"
            )
            self._apply_ptb(px, source, obs if obs is not None else start_ms)
            return

        if stored is not None:
            self._price_to_beat = float(stored["price"])
            self._price_to_beat_source = str(
                stored.get("source") or "open_twap_30s"
            )

    async def _lock_price_to_beat(self, *, btc: float | None) -> None:
        # Kept for call sites; ignores live `btc` so reload cannot overwrite PTB.
        del btc
        await self._maybe_capture_open_twap_for_next()
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
                self._condition_id = None
                self._token_up = None
                self._token_down = None
                self._window_start_ms = wall_window_ms
                self._window_end_ms = wall_window_ms + MARKET_DURATION_S * 1000
                self._price_to_beat = None
                self._holders_cache = None
            else:
                self._market = None
            return None

        market_id = str(market.get("id") or market.get("conditionId") or "")
        condition_id = str(market.get("conditionId") or "") or None
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
        prev_market_id = self._market_id
        if rolled and self._window_end_ms is not None:
            # Next window opens at this close — lock TWAP at that open as PTB.
            self._persist_open_twap(self._window_end_ms)
        self._market = market
        self._market_id = market_id
        self._condition_id = condition_id
        self._token_up = token_up
        self._token_down = token_down
        self._window_start_ms = start_ms
        self._window_end_ms = end_s * 1000
        if rolled:
            # Authoritative VPS copy of the market that just closed.
            if prev_market_id and prev_market_id != market_id:
                try:
                    await get_vps_sync().pull_market(prev_market_id, force=True)
                except Exception:
                    pass
            try:
                await get_vps_sync().ensure_active_market(market_id, force=True)
            except Exception:
                pass
            self._series.clear()
            self._series_market_id = market_id
            self._holders_cache = None
            self._price_to_beat = None
            self._price_to_beat_source = None
            self._fetch_live_open_px = None
            self._fetch_live_open_for = None
            self._fetch_live_open_obs = None
            # Prefer a good RTDS lock; fetch_live meta is provisional only.
            stored = ptb_store.get_price_to_beat(start_ms)
            if (
                stored is not None
                and ptb_store.is_good_sample(start_ms, stored.get("observed_ts"))
                and str(stored.get("source") or "") != "fetch_live_meta"
            ):
                self._price_to_beat = float(stored["price"])
                self._price_to_beat_source = str(
                    stored.get("source") or "open_twap_30s"
                )
            else:
                meta_hit = self._lookup_fetch_live_open(
                    market_id=market_id, window_start_ms=start_ms
                )
                if meta_hit is not None:
                    px, obs = meta_hit
                    if obs is not None and ptb_store.is_good_sample(start_ms, obs):
                        self._apply_ptb(px, "open_twap_30s", obs)
                    else:
                        self._price_to_beat = float(px)
                        self._price_to_beat_source = "fetch_live_meta"
                elif stored is not None:
                    self._price_to_beat = float(stored["price"])
                    self._price_to_beat_source = str(
                        stored.get("source") or "open_twap_30s"
                    )
        else:
            # Mid-window: keep local mirror of VPS prefix fresh (throttled).
            try:
                await get_vps_sync().ensure_active_market(market_id)
            except Exception:
                pass
        return market

    def _record_series_point(self, snap: dict[str, Any]) -> None:
        if snap.get("type") != "tick":
            return
        mid = snap.get("market_id")
        if not mid:
            return
        mid_s = str(mid)
        if self._series_market_id != mid_s:
            self._series.clear()
            self._series_market_id = mid_s
        up = snap.get("up_price")
        down = snap.get("down_price")
        if up is None and down is None and snap.get("btc_price") is None:
            return
        point = {
            "t": int(snap.get("timestamp") or time.time() * 1000),
            "up": float(up) if up is not None else None,
            "down": float(down) if down is not None else None,
            "btc": float(snap["btc_price"]) if snap.get("btc_price") is not None else None,
            "twap": float(snap["btc_twap_30s"])
            if snap.get("btc_twap_30s") is not None
            else None,
            "chainlink": float(snap["btc_chainlink"])
            if snap.get("btc_chainlink") is not None
            else None,
        }
        if self._series and int(self._series[-1]["t"]) == point["t"]:
            self._series[-1] = point
        else:
            self._series.append(point)

    def _twap_feed_series(self, start_ms: int | None) -> list[dict[str, Any]]:
        if start_ms is None:
            return []
        hist = self.twap.history_since(start_ms)
        by_t: dict[int, dict[str, Any]] = {}
        for ts, px in hist.get("twap") or []:
            by_t[int(ts)] = {"t": int(ts), "twap": float(px)}
        for ts, px in hist.get("chainlink") or []:
            cur = by_t.setdefault(int(ts), {"t": int(ts)})
            cur["chainlink"] = float(px)
        return [by_t[t] for t in sorted(by_t)]

    def series(
        self, market_id: str | None = None, *, lookback_ms: int = 180_000
    ) -> dict[str, Any]:
        """Chart backfill: fetch_live parquet + in-process buffer (+ RTDS hist)."""
        mid = str(market_id or self._market_id or "") or None
        start_ms = self._window_start_ms
        now_ms = int(time.time() * 1000)
        lookback = max(30_000, min(int(lookback_ms), 600_000))
        cutoff = now_ms - lookback
        if start_ms is not None:
            cutoff = max(int(start_ms) - 2_000, cutoff)

        parquet = load_fetch_live_series(mid) if mid else []
        feed = self._twap_feed_series(cutoff)
        buf = list(self._series) if (mid is None or mid == self._series_market_id) else []
        merged = merge_series(merge_series(parquet, feed), buf)
        merged = [p for p in merged if int(p["t"]) >= cutoff]
        # Drop open-book 1¢/99¢ stubs and break absurd one-tick flips.
        merged = scrub_leading_outcome_extremes(merged)
        merged = break_outcome_jumps(merged)
        return {
            "market_id": mid,
            "start_time": start_ms,
            "end_time": self._window_end_ms,
            "lookback_ms": lookback,
            "series": merged,
            "source": {
                "parquet": len(parquet),
                "twap_feed": len(feed),
                "buffer": len(buf),
            },
        }

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

        snap = self._with_twap(
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
        self._record_series_point(snap)
        return snap

    async def holders(self, *, limit: int = 20) -> dict[str, Any]:
        """Top Up/Down holders for the active market (cached ~0.3s)."""
        await self._ensure_market()
        now = time.time()
        if (
            self._holders_cache is not None
            and self._holders_cache.get("condition_id") == self._condition_id
            and now - self._holders_cache_at < 0.3
        ):
            return self._holders_cache

        cid = self._condition_id
        if not cid:
            empty = {
                "market_id": self._market_id,
                "condition_id": None,
                "updated_at": int(now * 1000),
                "live": True,
                "up": [],
                "down": [],
            }
            self._holders_cache = empty
            self._holders_cache_at = now
            return empty

        rows = await self.clients.get_holders(cid, limit=limit)
        up_token = str(self._token_up or "")
        down_token = str(self._token_down or "")

        def _norm(h: dict[str, Any]) -> dict[str, Any]:
            wallet = str(h.get("proxyWallet") or "")
            name = str(h.get("name") or "").strip()
            pseudo = str(h.get("pseudonym") or "").strip()
            public = bool(h.get("displayUsernamePublic"))
            if public and name:
                display = name
            elif pseudo:
                display = pseudo
            elif name:
                display = name
            elif wallet:
                display = f"{wallet[:6]}...{wallet[-4:]}"
            else:
                display = "—"
            amount = h.get("amount")
            try:
                shares = float(amount) if amount is not None else 0.0
            except (TypeError, ValueError):
                shares = 0.0
            return {
                "proxy_wallet": wallet,
                "display_name": display,
                "amount": shares,
                "profile_image": str(
                    h.get("profileImageOptimized") or h.get("profileImage") or ""
                ),
                "verified": bool(h.get("verified")),
                "outcome_index": h.get("outcomeIndex"),
            }

        up: list[dict[str, Any]] = []
        down: list[dict[str, Any]] = []
        for block in rows:
            token = str(block.get("token") or "")
            holders = [_norm(h) for h in (block.get("holders") or [])]
            holders.sort(key=lambda x: x["amount"], reverse=True)
            if token and token == up_token:
                up = holders
            elif token and token == down_token:
                down = holders
            else:
                # Fallback by outcomeIndex when token ids drift.
                idxs = {
                    h.get("outcomeIndex")
                    for h in (block.get("holders") or [])
                    if h.get("outcomeIndex") is not None
                }
                if idxs == {0} or (0 in idxs and 1 not in idxs and not up):
                    up = holders
                elif idxs == {1} or (1 in idxs and not down):
                    down = holders

        payload = {
            "market_id": self._market_id,
            "condition_id": cid,
            "updated_at": int(now * 1000),
            "live": True,
            "up": up,
            "down": down,
        }
        self._holders_cache = payload
        self._holders_cache_at = now
        return payload

    async def market_meta(self) -> dict[str, Any] | None:
        self.twap.ensure_started()
        market = await self._ensure_market(force=True)
        if not market:
            return None
        return {
            "type": "market",
            "live": True,
            "market_id": self._market_id,
            "condition_id": self._condition_id,
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
