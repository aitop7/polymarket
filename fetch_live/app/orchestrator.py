"""Wire feeds, snapshots, flush, and market lifecycle."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from app.binance.hub import BinanceHub
from app.config import settings
from app.depth_bands import build_orderbook_row
from app.discovery import Discovery
from app.polymarket.clob_rest import ClobRest
from app.polymarket.clob_ws import ClobMarketWs
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
        )
        self.clob_ws = ClobMarketWs(
            on_trade=self._on_pm_trade,
            on_book=None,
        )
        self.binance = BinanceHub(
            on_trade=self._on_btc_trade,
        )
        self._running = False
        self._tasks: list[asyncio.Task[Any]] = []

    async def _on_market_change(
        self, store: MarketStore, token_up: str | None, token_down: str | None
    ) -> None:
        self.resolver.track(store)
        self.clob_ws.set_subscriptions(
            market_id=store.market_id,
            token_up=token_up,
            token_down=token_down,
        )
        up_levels, down_levels = await self.clob_rest.seed_books(token_up, token_down)
        if token_up:
            self.clob_ws.seed_book(token_up, up_levels)
        if token_down:
            self.clob_ws.seed_book(token_down, down_levels)

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

    def _on_pm_trade(self, row: dict[str, Any]) -> None:
        store = self._store()
        if store is None:
            return
        store.append_pm_trade(row)
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
        # Floor to second for stable 1s keys
        ts = now_ms - (now_ms % 1000)
        if not store.in_window(ts):
            return

        if self.binance.book.ready:
            store.append_depth(self.binance.book.depth_row(ts))

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
            # also flush pending resolved stores tracked by resolver
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
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self.sessions.current is not None:
            self.sessions.current.flush(force=True)
        for store in list(self.resolver._pending.values()):
            store.flush(force=True)
        await self.binance.close()
        await self.clob_rest.close()
        await self.discovery.close()
        await self.twap.close()
        logger.info("fetch_live shutdown complete")
