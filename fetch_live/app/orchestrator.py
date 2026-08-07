"""Wire feeds, snapshots, flush, and market lifecycle."""

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
from app.polymarket.rtds_trades import RtdsTrades
from app.session.manager import SessionManager
from app.session.resolver import Resolver
from app.storage.market_store import MarketStore
from app.twap_open import TwapOpenResolver


class Orchestrator:
    def __init__(self) -> None:
        self.discovery = Discovery()
        self.twap = TwapOpenResolver()
        self.clob_rest = ClobRest()
        self.resolver = Resolver(self.discovery)
        self.sessions = SessionManager(
            self.discovery,
            self.twap,
            on_market_change=self._on_market_change,
            on_market_end=self._on_market_end,
        )
        # CLOB WS: order books only.
        # RTDS activity/trades: real-time fills + proxyWallet.
        # Data API: seed / end gap-fill only.
        self.clob_ws = ClobMarketWs(on_trade=None, on_book=None)
        self.rtds_trades = RtdsTrades(on_trade=self._on_rtds_trade)
        self.data_trades = DataApiTrades()
        self.binance = BinanceHub(
            on_trade=self._on_btc_trade,
        )
        self._running = False
        self._tasks: list[asyncio.Task[Any]] = []

    async def _on_market_change(
        self,
        store: MarketStore,
        token_up: str | None,
        token_down: str | None,
        condition_id: str = "",
    ) -> None:
        self.resolver.track(store)
        self.clob_ws.set_subscriptions(
            market_id=store.market_id,
            token_up=token_up,
            token_down=token_down,
        )
        cid = condition_id or str(store.meta.get("condition_id") or "")
        slug = str(store.meta.get("slug") or "")
        self.rtds_trades.set_market(
            slug=slug or None,
            condition_id=cid or None,
            token_up=token_up,
            token_down=token_down,
            start_ms=store.start_ms,
            end_ms=store.end_ms,
        )
        up_levels, down_levels = await self.clob_rest.seed_books(token_up, token_down)
        if token_up:
            self.clob_ws.seed_book(token_up, up_levels)
        if token_down:
            self.clob_ws.seed_book(token_down, down_levels)
        # Seed any trades already indexed before we subscribed
        if cid:
            try:
                rows = await self.data_trades.fetch_window(
                    condition_id=cid,
                    token_up=token_up,
                    token_down=token_down,
                    start_ms=store.start_ms,
                    end_ms=store.end_ms,
                    max_pages=20,
                )
                store.upsert_pm_trades(rows)
                store.flush()
                logger.info(
                    "Seeded {} Data API trades for market {}",
                    len(rows),
                    store.market_id,
                )
            except Exception as exc:
                logger.warning("Initial Data API trades fetch failed: {}", exc)

    async def _on_market_end(self, store: MarketStore) -> None:
        """Gap-fill from Data API before rolling off / deactivating."""
        cid = str(store.meta.get("condition_id") or "")
        if not cid:
            market = await self.discovery.get_market_by_slug(
                str(store.meta.get("slug") or "")
            )
            if market is None:
                market = await self.discovery.get_market_by_id(store.market_id)
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
        try:
            rows = await self.data_trades.fetch_window(
                condition_id=cid,
                token_up=str(store.meta.get("up_token_id") or "") or None,
                token_down=str(store.meta.get("down_token_id") or "") or None,
                start_ms=store.start_ms,
                end_ms=store.end_ms,
                max_pages=50,
            )
            store.upsert_pm_trades(rows)
            filled, n = store.wallet_fill_rate()
            store.flush(force=True)
            logger.info(
                "Final trades gap-fill market {} api_rows={} wallets={}/{}",
                store.market_id,
                len(rows),
                filled,
                n,
            )
        except Exception as exc:
            logger.warning(
                "Final Data API trades fetch failed for {}: {}", store.market_id, exc
            )

    def _store(self) -> MarketStore | None:
        return self.sessions.current

    def _on_btc_trade(
        self, agg_id: int, ts: int, price: float, qty: float, maker: bool
    ) -> None:
        store = self._store()
        if store is None:
            return
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
        store = self._store()
        if store is None:
            return
        store.upsert_pm_trade(row)
        if store.should_flush():
            store.flush()

    async def run(self) -> None:
        self._running = True
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.twap.ensure_started()
        logger.info("fetch_live starting data_dir={}", settings.data_dir.resolve())

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
                await self.sessions.tick()
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
        store = self._store()
        if store is None:
            return
        now_ms = int(time.time() * 1000)
        ts = now_ms - (now_ms % 1000)
        if not store.in_window(ts):
            return

        cl = self.twap.latest_chainlink()
        tw = self.twap.latest_twap()
        if cl is not None or tw is not None:
            store.append_chainlink_price(
                {
                    "timestamp": ts,
                    "Chainlink_BTC": float(cl[0]) if cl is not None else None,
                    "twap": float(tw[0]) if tw is not None else None,
                }
            )

        if self.binance.book.ready:
            mid = self.binance.book.mid_price()
            bands = build_binance_band_fields(
                self.binance.book.bids,
                self.binance.book.asks,
                float(mid) if mid is not None else 0.0,
            )
            store.append_binance_price_orderbook(
                {
                    "timestamp": ts,
                    "Binance_BTC": float(mid) if mid is not None else None,
                    **bands,
                }
            )

        up_tok = self.sessions.token_up
        down_tok = self.sessions.token_down
        up = self.clob_ws.get_book_levels(up_tok)
        down = self.clob_ws.get_book_levels(down_tok)
        row = build_orderbook_row(
            timestamp_ms=ts,
            up_bids=up["bids"],
            up_asks=up["asks"],
            down_bids=down["bids"],
            down_asks=down["asks"],
            up_price=self.clob_ws.last_up_price,
            down_price=self.clob_ws.last_down_price,
        )
        store.append_orderbook(row)

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(settings.flush_interval_s)
            store = self._store()
            if store is not None:
                store.flush()
            for pending in list(self.resolver._pending.values()):
                if pending is not store:
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

        if self.sessions.current is not None:
            await _shutdown_store(self.sessions.current)
        for store in list(self.resolver._pending.values()):
            if store is self.sessions.current:
                continue
            await _shutdown_store(store)
        await self.binance.close()
        await self.clob_rest.close()
        await self.data_trades.close()
        await self.discovery.close()
        await self.twap.close()
        logger.info("fetch_live shutdown complete")
