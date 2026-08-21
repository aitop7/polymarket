"""Wire feeds, snapshots, flush, and market lifecycle (5m + 15m in parallel)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from app.binance.hub import BinanceHub
from app.binance_bands import build_binance_band_fields
from app.config import settings
from app.depth_bands import build_orderbook_row
from app.discovery import Discovery
from app.polymarket.clob_rest import ClobRest
from app.polymarket.clob_ws import ClobMarketWs
from app.polymarket.data_api_trades import DataApiTrades
from app.polymarket.rtds_trades import RtdsMarketFilter, RtdsTrades
from app.series import ALL_SERIES
from app.session.manager import SessionManager
from app.session.resolver import Resolver
from app.storage.market_store import MarketStore
from app.trades_mode import get_trades_mode
from app.twap_open import TwapOpenResolver


class Orchestrator:
    def __init__(self) -> None:
        self.twap = TwapOpenResolver()
        self.clob_rest = ClobRest()
        # Shared HTTP discovery client pool — one Discovery per series.
        self.discoveries: dict[str, Discovery] = {
            s.key: Discovery(s) for s in ALL_SERIES
        }
        # Resolver needs any discovery for get_market_by_id / slug lookups.
        self.resolver = Resolver(self.discoveries["5m"])
        self.sessions: dict[str, SessionManager] = {}
        for series in ALL_SERIES:
            self.sessions[series.key] = SessionManager(
                self.discoveries[series.key],
                self.twap,
                series=series,
                on_market_change=self._on_market_change,
                on_market_end=self._on_market_end,
            )
        self.clob_ws = ClobMarketWs(on_trade=None, on_book=None)
        self.rtds_trades = RtdsTrades(on_trade=self._on_rtds_trade)
        self.data_trades = DataApiTrades()
        self.binance = BinanceHub(on_trade=self._on_btc_trade)
        self._running = False
        self._tasks: list[asyncio.Task[Any]] = []
        # token_id / market_id -> store for routing
        self._store_by_market: dict[str, MarketStore] = {}
        self._store_by_token: dict[str, MarketStore] = {}

    @property
    def discovery(self) -> Discovery:
        """Back-compat: primary (5m) discovery."""
        return self.discoveries["5m"]

    def _active_stores(self) -> list[MarketStore]:
        out: list[MarketStore] = []
        for sess in self.sessions.values():
            if sess.current is not None:
                out.append(sess.current)
        return out

    def _rebuild_routing(self) -> None:
        by_market: dict[str, MarketStore] = {}
        by_token: dict[str, MarketStore] = {}
        clob_markets: list[dict[str, Any]] = []
        rtds_markets: list[RtdsMarketFilter] = []
        for key, sess in self.sessions.items():
            store = sess.current
            if store is None:
                continue
            by_market[store.market_id] = store
            tok_up = sess.token_up
            tok_down = sess.token_down
            if tok_up:
                by_token[tok_up] = store
            if tok_down:
                by_token[tok_down] = store
            clob_markets.append(
                {
                    "market_id": store.market_id,
                    "token_up": tok_up,
                    "token_down": tok_down,
                }
            )
            cid = str(store.meta.get("condition_id") or "")
            slug = str(store.meta.get("slug") or "")
            rtds_markets.append(
                RtdsMarketFilter(
                    market_id=store.market_id,
                    slug=slug or None,
                    condition_id=cid or None,
                    token_up=tok_up,
                    token_down=tok_down,
                    start_ms=store.start_ms,
                    end_ms=store.end_ms,
                )
            )
        self._store_by_market = by_market
        self._store_by_token = by_token
        self.clob_ws.set_all_subscriptions(clob_markets)
        self.rtds_trades.set_markets(rtds_markets)

    async def _on_market_change(
        self,
        store: MarketStore,
        token_up: str | None,
        token_down: str | None,
        condition_id: str = "",
    ) -> None:
        self.resolver.track(store)
        self._rebuild_routing()
        up_levels, down_levels = await self.clob_rest.seed_books(token_up, token_down)
        if token_up:
            self.clob_ws.seed_book(token_up, up_levels)
        if token_down:
            self.clob_ws.seed_book(token_down, down_levels)
        mode = get_trades_mode()
        store.update_meta(trades_mode=mode)
        cid = condition_id or str(store.meta.get("condition_id") or "")
        if cid:
            try:
                rows = await self.data_trades.fetch_window(
                    condition_id=cid,
                    token_up=token_up,
                    token_down=token_down,
                    start_ms=store.start_ms,
                    end_ms=store.end_ms,
                    max_pages=20,
                    trades_mode=mode,
                )
                store.upsert_pm_trades(rows)
                store.flush()
                logger.info(
                    "Seeded {} Data API trades for market {} series={} mode={}",
                    len(rows),
                    store.market_id,
                    store.meta.get("series"),
                    mode,
                )
            except Exception as exc:
                logger.warning("Initial Data API trades fetch failed: {}", exc)

    async def _on_market_end(self, store: MarketStore) -> None:
        """Gap-fill from Data API before rolling off / deactivating."""
        cid = str(store.meta.get("condition_id") or "")
        disc = self._discovery_for_store(store)
        if not cid:
            market = await disc.get_market_by_slug(str(store.meta.get("slug") or ""))
            if market is None:
                market = await disc.get_market_by_id(store.market_id)
            if market:
                cid = str(market.get("conditionId") or market.get("condition_id") or "")
                if cid:
                    store.update_meta(condition_id=cid)
        if not cid:
            logger.warning(
                "No condition_id for market {}; skipping final trades fetch",
                store.market_id,
            )
            return
        mode = get_trades_mode()
        store.update_meta(trades_mode=mode)
        try:
            rows = await self.data_trades.fetch_window(
                condition_id=cid,
                token_up=str(store.meta.get("up_token_id") or "") or None,
                token_down=str(store.meta.get("down_token_id") or "") or None,
                start_ms=store.start_ms,
                end_ms=store.end_ms,
                max_pages=50,
                trades_mode=mode,
            )
            store.upsert_pm_trades(rows)
            filled, n = store.wallet_fill_rate()
            store.flush(force=True)
            logger.info(
                "Final trades gap-fill market {} series={} mode={} api_rows={} wallets={}/{}",
                store.market_id,
                store.meta.get("series"),
                mode,
                len(rows),
                filled,
                n,
            )
        except Exception as exc:
            logger.warning(
                "Final Data API trades fetch failed for {}: {}", store.market_id, exc
            )

    def _discovery_for_store(self, store: MarketStore) -> Discovery:
        key = str(store.meta.get("series") or "5m")
        return self.discoveries.get(key) or self.discoveries["5m"]

    def _on_btc_trade(
        self, agg_id: int, ts: int, price: float, qty: float, maker: bool
    ) -> None:
        for store in self._active_stores():
            store.try_btc_trade(
                agg_id=agg_id,
                timestamp=ts,
                price=price,
                quantity=qty,
                buyer_is_maker=maker,
            )
            if store.should_flush():
                store.flush()

    def _on_rtds_trade(self, row: dict[str, Any]) -> None:
        mid = str(row.get("market_id") or "")
        store = self._store_by_market.get(mid) if mid else None
        if store is None and len(self._store_by_market) == 1:
            store = next(iter(self._store_by_market.values()))
        if store is None:
            return
        store.upsert_pm_trade(row)
        if store.should_flush():
            store.flush()

    async def run(self) -> None:
        self._running = True
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.twap.ensure_started()
        logger.info(
            "fetch_live starting data_dir={} series={} trades_mode={}",
            settings.data_dir.resolve(),
            [s.key for s in ALL_SERIES],
            get_trades_mode(),
        )

        self._tasks = [
            asyncio.create_task(self.binance.run(), name="binance"),
            asyncio.create_task(self.clob_ws.run(), name="clob-ws"),
            asyncio.create_task(self.rtds_trades.run(), name="rtds-trades"),
            asyncio.create_task(self._discovery_loop(), name="discovery"),
            asyncio.create_task(self._snapshot_loop(), name="snapshots"),
            asyncio.create_task(self._flush_loop(), name="flush"),
            asyncio.create_task(self._resolve_loop(), name="resolve"),
        ]
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            raise
        finally:
            await self.shutdown()

    async def _discovery_loop(self) -> None:
        while self._running:
            try:
                for sess in self.sessions.values():
                    await sess.tick()
                self._rebuild_routing()
            except Exception as exc:
                logger.exception("discovery tick failed: {}", exc)
            await asyncio.sleep(settings.discovery_interval_s)

    async def _snapshot_loop(self) -> None:
        while self._running:
            try:
                await self._emit_snapshots()
            except Exception as exc:
                logger.exception("snapshot failed: {}", exc)
            await asyncio.sleep(settings.snapshot_interval_s)

    async def _emit_snapshots(self) -> None:
        stores = self._active_stores()
        if not stores:
            return
        now_ms = int(time.time() * 1000)
        ts = now_ms - (now_ms % 1000)

        stale_ms = 15_000
        cl = self.twap.latest_chainlink()
        tw = self.twap.latest_twap()
        cl_px = (
            float(cl[0])
            if cl is not None and ts - int(cl[1]) <= stale_ms
            else None
        )
        tw_px = (
            float(tw[0])
            if tw is not None and ts - int(tw[1]) <= stale_ms
            else None
        )
        binance_row: dict[str, Any] | None = None
        if self.binance.book.ready:
            mid = self.binance.book.mid_price()
            bands = build_binance_band_fields(
                self.binance.book.bids,
                self.binance.book.asks,
                float(mid) if mid is not None else 0.0,
            )
            binance_row = {
                "timestamp": ts,
                "Binance_BTC": float(mid) if mid is not None else None,
                **bands,
            }

        for key, sess in self.sessions.items():
            store = sess.current
            if store is None or not store.in_window(ts):
                continue
            if cl_px is not None or tw_px is not None:
                store.append_chainlink_price(
                    {
                        "timestamp": ts,
                        "Chainlink_BTC": cl_px,
                        "twap": tw_px,
                    }
                )
            if binance_row is not None:
                store.append_binance_price_orderbook(dict(binance_row))

            up_tok = sess.token_up
            down_tok = sess.token_down
            up = self.clob_ws.get_book_levels(up_tok)
            down = self.clob_ws.get_book_levels(down_tok)
            row = build_orderbook_row(
                timestamp_ms=ts,
                up_bids=up["bids"],
                up_asks=up["asks"],
                down_bids=down["bids"],
                down_asks=down["asks"],
                up_price=self.clob_ws.last_price(up_tok),
                down_price=self.clob_ws.last_price(down_tok),
            )
            store.append_orderbook(row)

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(settings.flush_interval_s)
            active = set(self._active_stores())
            for store in active:
                store.flush()
            for pending in list(self.resolver._pending.values()):
                if pending not in active:
                    pending.flush()

    async def _resolve_loop(self) -> None:
        while self._running:
            try:
                await self.resolver.poll_once()
            except Exception as exc:
                logger.exception("resolve poll failed: {}", exc)
            await asyncio.sleep(settings.resolve_poll_interval_s)

    async def shutdown(self) -> None:
        if not self._running and not self._tasks:
            return
        self._running = False
        self.clob_ws.stop()
        self.rtds_trades.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        now = int(time.time() * 1000)

        async def _shutdown_store(store: MarketStore) -> None:
            try:
                await self._on_market_end(store)
            except Exception as exc:
                logger.warning(
                    "Shutdown trades fetch failed {}: {}", store.market_id, exc
                )
            if store.meta.get("btc_close_price") is None and now >= store.end_ms:
                try:
                    close_px = await self.twap.resolve_close_price(store.end_ms)
                    store.update_meta(btc_close_price=close_px, active=False)
                except Exception as exc:
                    logger.warning(
                        "Shutdown close TWAP failed {}: {}", store.market_id, exc
                    )
                    store.update_meta(active=False)
            else:
                store.update_meta(active=False)
            store.flush(force=True)

        seen: set[str] = set()
        for sess in self.sessions.values():
            if sess.current is not None and sess.current.market_id not in seen:
                seen.add(sess.current.market_id)
                await _shutdown_store(sess.current)
        for store in list(self.resolver._pending.values()):
            if store.market_id in seen:
                continue
            seen.add(store.market_id)
            await _shutdown_store(store)
        await self.binance.close()
        await self.clob_rest.close()
        await self.data_trades.close()
        for disc in self.discoveries.values():
            await disc.close()
        await self.twap.close()
        logger.info("fetch_live shutdown complete")
