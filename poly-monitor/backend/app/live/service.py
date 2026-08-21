"""Assemble live snapshots for the monitor UI."""

from __future__ import annotations

import json
import time
import asyncio
from collections import deque
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.pricing import quotes_from_up_buy
from app.core.series import ALL_SERIES, MarketSeries, get_series
from app.live.clients import LiveClients, parse_token_ids, window_start_unix
from app.live import ptb_store
from app.live.fetch_live_series import (
    break_outcome_jumps,
    load_fetch_live_series,
    merge_series,
    scrub_leading_outcome_extremes,
)
from app.live.activity_feed import ActivityFeed
from app.live.twap_feed import get_twap_feed

# Don't lock PTB before the open boundary; keep refining near open for RTDS.
_PTB_REFINE_MS = 45_000
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
    def __init__(self, series: MarketSeries | str | None = None) -> None:
        self.series = series if isinstance(series, MarketSeries) else get_series(series)
        self.clients = LiveClients()
        self.twap = get_twap_feed(self.series.rtds_symbol)
        # Per-series activity feed so series do not fight over filters.
        self.activity = ActivityFeed()
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
        self._last_btc_price: float | None = None
        self._last_binance_book: dict[str, Any] | None = None
        self._last_snapshot: dict[str, Any] | None = None
        self._last_snapshot_at = 0.0
        self._holders_cache: dict[str, Any] | None = None
        self._holders_cache_at = 0.0
        self._ptb_refine_task: asyncio.Task[None] | None = None
        # Start RTDS early so open TWAP (Price to Beat) is ready at market open.
        self.twap.ensure_started()
        self.activity.ensure_started()

    @property
    def duration_s(self) -> int:
        return self.series.duration_s

    @property
    def duration_ms(self) -> int:
        return self.series.duration_ms

    async def close(self) -> None:
        if self._ptb_refine_task is not None and not self._ptb_refine_task.done():
            self._ptb_refine_task.cancel()
            try:
                await self._ptb_refine_task
            except asyncio.CancelledError:
                pass
            self._ptb_refine_task = None
        self.twap.stop()
        self.activity.stop()
        await self.clients.close()

    def drain_activity(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.activity.drain(limit=limit)

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

            from app.core.live_dataset import resolve_chainlink_path

            candidates: list[Path] = []
            resolved = resolve_chainlink_path(market_dir)
            if resolved is not None:
                candidates.append(resolved)
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
                            other_cl = resolve_chainlink_path(other)
                            if other_cl is not None:
                                candidates.append(other_cl)
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

    def _apply_gamma_ptb_from_market(self, market: dict[str, Any] | None) -> bool:
        """Lock Polymarket UI strike from eventMetadata.priceToBeat when present."""
        if not market:
            return False
        raw = market.get("priceToBeat")
        if raw is None:
            return False
        try:
            price = float(raw)
        except (TypeError, ValueError):
            return False
        if not (price > 0):
            return False
        start = int(self._window_start_ms or 0) or None
        self._price_to_beat = price
        self._price_to_beat_source = "gamma_price_to_beat"
        if start is not None:
            ptb_store.set_price_to_beat(
                start,
                price,
                source="gamma_price_to_beat",
                observed_ts=start,
            )
            self._sync_meta_open_price(price)
        return True

    def _sync_meta_open_price(self, price: float) -> None:
        """Best-effort: keep fetch_live meta.json btc_open_price on Gamma PTB."""
        mid = str(self._market_id or "").strip()
        if not mid or self._window_start_ms is None:
            return
        try:
            from app.core.live_dataset import find_live_market_dir

            d = find_live_market_dir(mid)
            if d is None:
                return
            meta_path = d / "meta.json"
            if not meta_path.is_file():
                return
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                return
            meta["btc_open_price"] = float(price)
            meta["btc_open_source"] = "gamma_price_to_beat"
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except Exception:
            return

    def _configure_twap_lookback(self, market: dict[str, Any] | None) -> None:
        lookback = None
        if market:
            lookback = market.get("twap_lookback_seconds")
            cfg = market.get("cryptoMarketConfig")
            if lookback is None and isinstance(cfg, dict):
                lookback = cfg.get("twapLookbackSeconds")
        self.twap.set_lookback_seconds(lookback if lookback is not None else 60)

    def _persist_open_twap(self, window_start_ms: int) -> None:
        """
        Persist RTDS sample only if it is already close to T0.
        Early/pre-open samples must not hard-lock Price To Beat.
        Never overwrites Gamma PTB.
        """
        start = int(window_start_ms)
        stored = ptb_store.get_price_to_beat(start)
        if stored is not None and ptb_store.is_gamma_source(stored.get("source")):
            return
        hit = self.twap.twap_at_close(start)
        if hit is None:
            return
        price, obs_ts = hit
        if not ptb_store.is_good_sample(start, obs_ts):
            return
        source = (
            "open_twap_60s" if self.twap.lookback_seconds >= 45 else "open_twap_30s"
        )
        ptb_store.set_price_to_beat(
            start, price, source=source, observed_ts=obs_ts
        )

    async def _fetch_open_price(
        self, window_start_ms: int, *, wait_s: float = 3.0, allow_computed: bool = True
    ) -> tuple[float, str, int] | None:
        """
        Fallback Price To Beat when Gamma eventMetadata.priceToBeat is missing.

        Primary: RTDS TWAP (30s or 60s per market config) nearest start_time.
        Fallback: Binance REST aggTrades TWAP (provisional only).
        """
        start_ms = int(window_start_ms)
        hit = await self.twap.resolve_twap_at(start_ms, wait_s=wait_s)
        if hit is not None:
            price, obs_ts = hit
            source = (
                "open_twap_60s"
                if self.twap.lookback_seconds >= 45
                else "open_twap_30s"
            )
            return float(price), source, int(obs_ts)

        # Last resort only — Binance ≠ Polymarket Chainlink (and can be slow).
        if not allow_computed:
            return None
        computed = await self.clients.compute_twap_30s_ending_at(
            start_ms, symbol=self.series.binance_symbol
        )
        if computed is not None:
            source = (
                "open_twap_60s_computed"
                if self.twap.lookback_seconds >= 45
                else "open_twap_30s_computed"
            )
            return float(computed), source, start_ms
        return None

    async def _maybe_capture_open_twap_for_next(self, *, wait_s: float = 0.0) -> None:
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
        if (
            stored is not None
            and (
                ptb_store.is_gamma_source(stored.get("source"))
                or (
                    ptb_store.is_rtds_source(stored.get("source"))
                    and ptb_store.is_good_sample(start, stored.get("observed_ts"))
                )
            )
        ):
            return
        fetched = await self._fetch_open_price(
            start, wait_s=wait_s, allow_computed=wait_s > 0
        )
        if fetched is None:
            return
        price, source, obs_ts = fetched
        # Skip writing provisional Binance on top of nothing if we can still wait for RTDS.
        if ptb_store.is_provisional_source(source) and now_ms <= start + 10_000:
            return
        ptb_store.set_price_to_beat(
            start, price, source=source, observed_ts=obs_ts
        )

    async def _resolve_price_to_beat(
        self, window_start_ms: int, *, wait_s: float = 0.0, allow_computed: bool = False
    ) -> None:
        """
        Price To Beat = Polymarket eventMetadata.priceToBeat when available;
        else RTDS Chainlink TWAP nearest window start.

        wait_s/allow_computed must stay 0 on the live tick path so Current Price
        (TWAP) keeps updating at the configured fetch interval.
        """
        start_ms = int(window_start_ms)
        now_ms = int(time.time() * 1000)

        # Prefer Gamma UI strike whenever the active market carries it.
        if self._apply_gamma_ptb_from_market(self._market):
            return

        stored = ptb_store.get_price_to_beat(start_ms)
        stored_source = str((stored or {}).get("source") or "")
        if stored is not None and ptb_store.is_gamma_source(stored_source):
            self._price_to_beat = float(stored["price"])
            self._price_to_beat_source = "gamma_price_to_beat"
            return

        provisional = stored is None or ptb_store.is_provisional_source(stored_source)
        good_rtds = (
            stored is not None
            and ptb_store.is_rtds_source(stored_source)
            and ptb_store.is_good_sample(start_ms, stored.get("observed_ts"))
        )
        refining = 0 <= (now_ms - start_ms) <= _PTB_REFINE_MS

        # Finalized RTDS lock — only re-check during the refine window.
        if good_rtds and not refining:
            self._price_to_beat = float(stored["price"])
            self._price_to_beat_source = stored_source or "open_twap_30s"
            return

        if stored is not None and self._price_to_beat is None:
            self._price_to_beat = float(stored["price"])
            self._price_to_beat_source = stored_source or "open_twap_30s"

        if now_ms < start_ms:
            # Not open yet — surface provisional values but do not hard-lock early.
            meta_hit = self._lookup_fetch_live_open(
                market_id=self._market_id, window_start_ms=start_ms
            )
            if meta_hit is not None:
                px, obs = meta_hit
                self._price_to_beat = float(px)
                self._price_to_beat_source = (
                    "open_twap_30s"
                    if obs is not None and obs != start_ms
                    else "fetch_live_meta"
                )
            return

        # Non-blocking RTDS sample (and optional Binance) — never stall ticks.
        if provisional or refining or self._price_to_beat is None:
            fetched = await self._fetch_open_price(
                start_ms, wait_s=wait_s, allow_computed=allow_computed
            )
            if fetched is not None:
                price, source, obs_ts = fetched
                if ptb_store.is_rtds_source(source) or provisional or self._price_to_beat is None:
                    wrote = ptb_store.set_price_to_beat(
                        start_ms, price, source=source, observed_ts=obs_ts
                    )
                    if wrote or self._price_to_beat is None or (
                        provisional and ptb_store.is_rtds_source(source)
                    ):
                        self._price_to_beat = price
                        self._price_to_beat_source = source
                        if ptb_store.is_rtds_source(source):
                            return

        # Fallbacks when RTDS missed the open boundary (tick path: in-memory only).
        if (self._price_to_beat is None or provisional) and wait_s > 0:
            meta_hit = self._lookup_fetch_live_open(
                market_id=self._market_id, window_start_ms=start_ms
            )
            if meta_hit is not None:
                px, obs = meta_hit
                source = (
                    "open_twap_30s"
                    if obs is not None and ptb_store.is_good_sample(start_ms, obs)
                    else "fetch_live_meta"
                )
                # Never let meta overwrite a better in-memory RTDS value.
                if self._price_to_beat is None or ptb_store.is_provisional_source(
                    self._price_to_beat_source
                ):
                    self._apply_ptb(px, source, obs if obs is not None else start_ms)
                    return

        if stored is not None and self._price_to_beat is None:
            self._price_to_beat = float(stored["price"])
            self._price_to_beat_source = stored_source or "open_twap_30s"

    async def _refine_ptb_background(self, window_start_ms: int) -> None:
        """Retry RTDS open lock without blocking the live tick loop."""
        start_ms = int(window_start_ms)
        try:
            # One off-tick disk lookup for fetch_live meta (never on snapshot path).
            if self._price_to_beat is None or ptb_store.is_provisional_source(
                self._price_to_beat_source
            ):
                meta_hit = await asyncio.to_thread(
                    self._lookup_fetch_live_open,
                    market_id=self._market_id,
                    window_start_ms=start_ms,
                )
                if meta_hit is not None and (
                    self._price_to_beat is None
                    or ptb_store.is_provisional_source(self._price_to_beat_source)
                ):
                    px, obs = meta_hit
                    source = (
                        "open_twap_30s"
                        if obs is not None and ptb_store.is_good_sample(start_ms, obs)
                        else "fetch_live_meta"
                    )
                    self._apply_ptb(px, source, obs if obs is not None else start_ms)

            for i in range(20):
                if self._apply_gamma_ptb_from_market(self._market):
                    return
                wait_s = 2.0 if i < 8 else 0.5
                allow_computed = i >= 6
                await self._resolve_price_to_beat(
                    start_ms, wait_s=wait_s, allow_computed=allow_computed
                )
                stored = ptb_store.get_price_to_beat(start_ms)
                if stored is not None and ptb_store.is_gamma_source(stored.get("source")):
                    self._price_to_beat = float(stored["price"])
                    self._price_to_beat_source = "gamma_price_to_beat"
                    return
                if (
                    stored is not None
                    and ptb_store.is_rtds_source(stored.get("source"))
                    and ptb_store.is_good_sample(start_ms, stored.get("observed_ts"))
                ):
                    self._price_to_beat = float(stored["price"])
                    self._price_to_beat_source = str(stored.get("source") or "open_twap_30s")
                    return
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _lock_price_to_beat(self, *, btc: float | None) -> None:
        # Kept for call sites; ignores live `btc` so reload cannot overwrite PTB.
        del btc
        # Non-blocking on tick path — never stall live TWAP / Current Price updates.
        await self._maybe_capture_open_twap_for_next(wait_s=0.0)
        if self._window_start_ms is None:
            return
        await self._resolve_price_to_beat(
            self._window_start_ms, wait_s=0.0, allow_computed=False
        )

    async def _ensure_market(self, *, force: bool = False) -> dict[str, Any] | None:
        now = time.time()
        dur_s = self.duration_s
        wall_window_ms = window_start_unix(now, duration_s=dur_s) * 1000
        need = force or self._market is None or (now - self._last_discover_s) > 15
        if self._window_end_ms is not None and now * 1000 >= self._window_end_ms:
            need = True
        # New slot on the wall clock → force rediscovery even if Gamma lags.
        if self._window_start_ms is not None and wall_window_ms != self._window_start_ms:
            need = True
        if not need:
            return self._market

        market = await self.clients.discover_active_updown(self.series)
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
                self._window_end_ms = wall_window_ms + self.duration_ms
                self._price_to_beat = None
                self._holders_cache = None
            else:
                self._market = None
            return None

        market_id = str(market.get("id") or market.get("conditionId") or "")
        condition_id = str(market.get("conditionId") or "") or None
        slug = str(market.get("slug") or "")
        token_up, token_down = parse_token_ids(market)

        match = self.series.slug_re.match(slug)
        if match:
            start_s = int(match.group(1))
        else:
            start_s = window_start_unix(now, duration_s=dur_s)
        end_s = start_s + dur_s
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
        self._configure_twap_lookback(market)
        if rolled:
            # Local history is refreshed by the 1-minute VPS sync loop (not mid-live pulls).
            self._series.clear()
            self._series_market_id = market_id
            self._holders_cache = None
            self._price_to_beat = None
            self._price_to_beat_source = None
            self._fetch_live_open_px = None
            self._fetch_live_open_for = None
            self._fetch_live_open_obs = None
            # Prefer Polymarket UI strike, then a good RTDS lock.
            if not self._apply_gamma_ptb_from_market(market):
                stored = ptb_store.get_price_to_beat(start_ms)
                if (
                    stored is not None
                    and ptb_store.is_gamma_source(stored.get("source"))
                ):
                    self._price_to_beat = float(stored["price"])
                    self._price_to_beat_source = "gamma_price_to_beat"
                elif (
                    stored is not None
                    and ptb_store.is_rtds_source(stored.get("source"))
                    and ptb_store.is_good_sample(start_ms, stored.get("observed_ts"))
                ):
                    self._price_to_beat = float(stored["price"])
                    self._price_to_beat_source = str(
                        stored.get("source") or "open_twap_30s"
                    )
                elif stored is not None:
                    # Disk/parquet lookup is deferred to background refine — scanning
                    # fetch_live on the tick path freezes Current Price updates.
                    self._price_to_beat = float(stored["price"])
                    self._price_to_beat_source = str(
                        stored.get("source") or "open_twap_30s"
                    )
        else:
            # Same market — still refresh Gamma strike / lookback if Gamma caught up.
            self._apply_gamma_ptb_from_market(market)
        # Keep RTDS activity filter on the active market (clear tape on roll).
        self.activity.set_market(
            slug=slug,
            condition_id=condition_id,
            token_up=token_up,
            token_down=token_down,
            start_ms=start_ms,
            end_ms=end_s * 1000,
            clear=rolled and prev_market_id is not None,
        )
        if rolled:
            # Refine PTB from RTDS in the background — never block live ticks.
            if self._ptb_refine_task is not None and not self._ptb_refine_task.done():
                self._ptb_refine_task.cancel()
            self._ptb_refine_task = asyncio.create_task(
                self._refine_ptb_background(start_ms),
                name=f"ptb-refine-{start_ms}",
            )
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
        # 1s buckets match fetch_live parquet so live ticks overlay seed points
        # instead of interleaving ms samples with flat 1s Binance rows.
        raw_t = int(snap.get("timestamp") or time.time() * 1000)
        point = {
            "t": (raw_t // 1000) * 1000,
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
            prev = self._series[-1]
            for key in ("up", "down", "btc", "twap", "chainlink"):
                if point[key] is None and prev.get(key) is not None:
                    point[key] = prev[key]
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
        if mid:
            from app.core.trade_volume import attach_volumes_to_series, volumes_for_market_id

            # Live: only attach volume onto real in-bucket samples (no ghost bars).
            merged = attach_volumes_to_series(
                merged,
                volumes_for_market_id(mid),
                synthesize_missing=False,
            )
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

        async def _btc() -> float | None:
            try:
                px = await self.clients.get_btc_price(self.series.binance_symbol)
                self._last_btc_price = float(px)
                return self._last_btc_price
            except Exception:
                # Keep ticks flowing (TWAP/book) when Binance REST is saturated
                # by bulk repair; reuse last good mid so the chart doesn't freeze.
                return self._last_btc_price

        async def _book(token: str | None) -> dict[str, Any]:
            if not token:
                return {"bids": [], "asks": []}
            try:
                return await self.clients.get_order_book(token)
            except Exception:
                return {"bids": [], "asks": []}

        async def _binance_book() -> dict[str, Any] | None:
            try:
                depth = await self.clients.get_btc_depth(
                    limit=1000, symbol=self.series.binance_symbol
                )
                self._last_binance_book = depth
                return depth
            except Exception:
                return self._last_binance_book

        up_book: dict[str, Any] = {"bids": [], "asks": []}
        down_book: dict[str, Any] = {"bids": [], "asks": []}
        binance_book: dict[str, Any] | None = None
        if market is None or not self._token_up:
            btc, binance_book = await asyncio.gather(_btc(), _binance_book())
        else:
            btc, up_book, down_book, binance_book = await asyncio.gather(
                _btc(),
                _book(self._token_up),
                _book(self._token_down),
                _binance_book(),
            )
        if btc is None:
            return self._with_twap(
                {
                    "type": "error",
                    "message": f"{self.series.asset} price unavailable",
                    "timestamp": now_ms,
                    "binance_book": binance_book,
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
                        "note": f"No active {self.series.slug_prefix} market found",
                        "up": None,
                        "down": None,
                    },
                    "binance_book": binance_book,
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
                "binance_book": binance_book,
            }
        )
        self._record_series_point(snap)
        self._last_snapshot = snap
        self._last_snapshot_at = time.monotonic()
        return snap

    async def ensure_snapshot(self, *, max_age_s: float = 0.75) -> dict[str, Any]:
        """Return a recent snapshot, refreshing only when the cache is stale.

        The direction-prediction endpoint polls sub-second; reusing a <1s-old
        book avoids paying REST depth/CLOB latency on every request.
        """
        age = time.monotonic() - self._last_snapshot_at
        if (
            self._last_snapshot is not None
            and age < max(0.05, float(max_age_s))
            and str(self._last_snapshot.get("market_id") or "") == str(self._market_id or "")
        ):
            return self._last_snapshot
        return await self.snapshot()

    def prediction_inputs(self, *, lookback_ms: int = 300_000) -> dict[str, Any]:
        """In-memory series + last snapshot only — no network / parquet I/O.

        Used by the high-frequency direction-prediction endpoint so scoring stays
        on the live clock instead of waiting for REST books or disk sync.
        """
        mid = str(self._market_id or "") or None
        now_ms = int(time.time() * 1000)
        lookback = max(30_000, min(int(lookback_ms), 600_000))
        cutoff = now_ms - lookback
        if self._window_start_ms is not None:
            cutoff = max(int(self._window_start_ms) - 2_000, cutoff)

        feed = self._twap_feed_series(cutoff)
        buf = list(self._series) if (mid is None or mid == self._series_market_id) else []
        merged = merge_series(feed, buf)
        merged = [p for p in merged if int(p["t"]) >= cutoff]
        merged = scrub_leading_outcome_extremes(merged)
        merged = break_outcome_jumps(merged)

        snap = self._last_snapshot
        if isinstance(snap, dict) and mid and str(snap.get("market_id") or "") not in ("", mid):
            snap = None
        if snap is None and mid:
            # Minimal stub so feature engineering still has window bounds / mids.
            last = merged[-1] if merged else {}
            snap = {
                "timestamp": int(last.get("t") or now_ms),
                "market_id": mid,
                "start_time": self._window_start_ms,
                "end_time": self._window_end_ms,
                "btc_price": last.get("btc") if last else self._last_btc_price,
                "up_price": last.get("up") if last else None,
                "down_price": last.get("down") if last else None,
                "btc_chainlink": last.get("chainlink") if last else None,
                "btc_twap_30s": last.get("twap") if last else None,
                "binance_book": self._last_binance_book,
            }

        return {
            "market_id": mid,
            "start_time": self._window_start_ms,
            "end_time": self._window_end_ms,
            "series": merged,
            "snapshot": snap,
        }

    async def holders(self, *, limit: int = 20) -> dict[str, Any]:
        """Top Up/Down holders for the active market (cached ~0.15s)."""
        await self._ensure_market()
        now = time.time()
        if (
            self._holders_cache is not None
            and self._holders_cache.get("condition_id") == self._condition_id
            and now - self._holders_cache_at < 0.15
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
            "series": self.series.key,
            "asset": self.series.asset,
            "binance_symbol": self.series.binance_symbol,
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


_LIVE: dict[str, LiveMarketService] = {}


def get_live_service(series: str | MarketSeries | None = None) -> LiveMarketService:
    """Return the live service for a series (default 5m). Both series stay warm once started."""
    s = series if isinstance(series, MarketSeries) else get_series(series)
    key = s.key
    svc = _LIVE.get(key)
    if svc is None:
        svc = LiveMarketService(s)
        _LIVE[key] = svc
    return svc


def warm_all_live_services() -> list[LiveMarketService]:
    """Ensure live services for all series exist (called from app lifespan)."""
    return [get_live_service(s.key) for s in ALL_SERIES]
