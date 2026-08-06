from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from app.api.binance import BinanceClient
from app.api.pmxt_client import PmxtClient
from app.api.polymarket import PolymarketClient
from app.config import settings
from app.storage import store
from app.storage.market_sessions import sessions
from app.storage.markets import markets
from app.utils.logger import logger
from app.utils.markets import (
    filter_updown_rows,
    is_updown_market,
    iter_5m_slugs,
    parse_updown_window,
    resolve_market_window,
)
from app.utils.progress import ProgressBar
from app.utils.time import datetime_to_ms, ms_to_datetime, utcnow


class HistorySynchronizer:
    """
    Backfill into one compressed parquet file per UTC day
    (many 5m markets share the same day file).

    --lookback-days N means: every btc-updown-5m slot in the last N days
    (N*24*12 markets), not \"N markets\".
    """

    def __init__(self, lookback_days: int | None = None) -> None:
        self.lookback_days = lookback_days if lookback_days is not None else settings.history_lookback_days
        self.binance = BinanceClient()
        self.poly = PolymarketClient()
        self.pmxt = PmxtClient()
        self._market_sem = asyncio.Semaphore(max(1, settings.market_concurrency))

    async def close(self) -> None:
        await self.binance.close()
        await self.poly.close()
        await self.pmxt.close()

    async def sync_all(self) -> dict[str, int]:
        expected = len(iter_5m_slugs(self.lookback_days))
        logger.info(
            "Starting history sync lookback_days={} (~{} five-minute slots) concurrency={}",
            self.lookback_days,
            expected,
            settings.market_concurrency,
        )
        print(
            f"Resolving {expected} five-minute markets over last {self.lookback_days} day(s)...",
            file=sys.stderr,
            flush=True,
        )
        resolved = await self.sync_markets()
        print(
            f"Resolved {len(resolved)}/{expected} markets. Downloading data...",
            file=sys.stderr,
            flush=True,
        )
        files = await self.sync_each_market(resolved)
        results = {"markets": len(resolved), "market_files": files, "slots": expected}
        logger.info("History sync complete: {}", results)
        return results

    async def sync_markets(self) -> list[Any]:
        """Enumerate every 5m slug in the lookback window and resolve via Gamma."""
        slugs = iter_5m_slugs(self.lookback_days)
        if not slugs:
            return []

        rows: list[dict[str, Any]] = []
        done = 0
        lock = asyncio.Lock()
        progress = ProgressBar(len(slugs), prefix="Resolve")
        progress.update(0, current="starting...", written=0)
        sem = asyncio.Semaphore(max(1, settings.download_concurrency))

        async def _resolve(slug: str) -> dict[str, Any] | None:
            nonlocal done
            async with sem:
                try:
                    raw = await self.poly.get_market_by_slug(slug)
                except Exception as exc:
                    logger.debug("slug resolve failed {}: {}", slug, exc)
                    raw = None
                row = None
                if raw:
                    row = self.poly.normalize_market(raw)
                    start, end = parse_updown_window(slug)
                    if start:
                        row["start_time"] = start
                    if end:
                        row["end_time"] = end
                        row["settlement_time"] = row.get("settlement_time") or end
                    row["slug"] = slug
                async with lock:
                    done += 1
                    if row:
                        rows.append(row)
                    progress.update(done, current=slug, written=len(rows))
                return row

        try:
            await asyncio.gather(*[_resolve(s) for s in slugs])
        finally:
            progress.close(
                final_msg=f"Resolved {len(rows)}/{len(slugs)} markets that exist on Polymarket"
            )

        filtered = filter_updown_rows(rows)
        # Don't finalize empty session files for closed markets during history resolve
        markets.upsert_many(filtered, finalize_closed=False)
        store.save_checkpoint("markets", last_timestamp=utcnow())
        by_id = {r["market_id"] for r in filtered}
        return [m for m in markets.all() if m.market_id in by_id and is_updown_market(m.slug)]

    async def sync_each_market(self, resolved_markets: list[Any] | None = None) -> int:
        all_markets = list(resolved_markets or [])
        total = len(all_markets)
        if total == 0:
            print(
                "No 5m up/down markets resolved in lookback window. Check network, then retry.",
                file=sys.stderr,
                flush=True,
            )
            return 0

        done = 0
        buffered = 0
        lock = asyncio.Lock()
        progress = ProgressBar(total, prefix="Download")
        # show 0% immediately so it never looks "paused"
        progress.update(0, current="starting...", written=0)

        async def _one(market: Any) -> bool:
            nonlocal done, buffered
            async with self._market_sem:
                slug = str(market.slug or market.market_id)
                async with lock:
                    progress.update(done, current=f"fetching {slug}", written=buffered)
                try:
                    rows = await self._build_market_rows(market)
                    ok = False
                    if rows:
                        n = sessions.buffer_market_rows(market.market_id, market.slug, rows)
                        ok = n > 0
                    async with lock:
                        done += 1
                        if ok:
                            buffered += 1
                        progress.update(done, current=slug, written=buffered)
                    return ok
                except Exception as exc:
                    async with lock:
                        done += 1
                        progress.update(done, current=slug, written=buffered)
                    logger.warning("History sync failed for {}: {}", market.market_id, exc)
                    return False

        try:
            await asyncio.gather(*[_one(m) for m in all_markets])
            if buffered:
                progress.update(done, current="writing daily parquet...", written=buffered)
                paths = await asyncio.to_thread(sessions.flush_history_buffer)
                logger.info("Flushed {} daily parquet file(s)", len(paths))
        finally:
            pct = 100.0 * done / total if total else 100.0
            progress.close(
                final_msg=f"Download complete: {buffered}/{total} markets buffered, written once per day ({pct:.1f}%)"
            )
        return buffered

    async def _build_market_rows(self, market: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = [
            {
                "record_type": "meta",
                "timestamp": market.start_time or utcnow(),
                "market_id": market.market_id,
                "slug": market.slug,
                **market.as_dict(),
            }
        ]

        # Critical: only the 5-minute window (never multi-week long markets)
        start, end = resolve_market_window(market, lookback_days=self.lookback_days)
        token = market.token_yes
        if not token:
            return rows

        if settings.pmxt_enabled:
            btc_task = asyncio.create_task(self._btc_bars(start, end, market))
            books_task = asyncio.create_task(self._pmxt_orderbooks(token, start, end, market))
            trades_task = asyncio.create_task(self._pmxt_trades(token, start, end, market))
            ohlcv_task = asyncio.create_task(self._pmxt_ohlcv(token, start, end, market))
            btc_rows, book_rows, trade_rows, ohlcv_rows = await asyncio.gather(
                btc_task, books_task, trades_task, ohlcv_task
            )
            rows.extend(btc_rows)
            rows.extend(book_rows)
            rows.extend(trade_rows)
            rows.extend(ohlcv_rows)
        else:
            btc_rows, hist_rows = await asyncio.gather(
                self._btc_bars(start, end, market),
                self._clob_price_history(token, start, end, market),
            )
            rows.extend(btc_rows)
            rows.extend(hist_rows)

        return rows

    async def _pmxt_orderbooks(
        self,
        token: str,
        start: datetime,
        end: datetime,
        market: Any,
    ) -> list[dict[str, Any]]:
        windows: list[tuple[datetime, datetime]] = []
        cursor = start
        # 5m markets fit in one window; keep chunking for longer markets
        chunk = timedelta(minutes=15)
        while cursor < end:
            until = min(cursor + chunk, end)
            windows.append((cursor, until))
            cursor = until

        async def _chunk(window: tuple[datetime, datetime]) -> list[dict[str, Any]]:
            since, until = window
            try:
                books = await self.pmxt.fetch_order_book(
                    token,
                    since=datetime_to_ms(since),
                    until=datetime_to_ms(until),
                    limit=1000,
                )
            except Exception as exc:
                logger.debug("PMXT books {} {}-{}: {}", market.slug, since, until, exc)
                return []
            if not isinstance(books, list):
                books = [books] if books else []
            out: list[dict[str, Any]] = []
            for book in books:
                ts_raw = book.get("timestamp")
                ts = ms_to_datetime(ts_raw) if ts_raw is not None else until
                bids = (book.get("bids") or [])[:20]
                asks = (book.get("asks") or [])[:20]
                best_bid = float(bids[0]["price"]) if bids else None
                best_ask = float(asks[0]["price"]) if asks else None
                spread = (
                    best_ask - best_bid
                    if best_bid is not None and best_ask is not None
                    else None
                )
                out.append(
                    {
                        "record_type": "orderbook",
                        "timestamp": ts,
                        "market_id": market.market_id,
                        "slug": market.slug,
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "spread": spread,
                        "book_json": {"bids": bids, "asks": asks, "asset_id": token},
                    }
                )
            return out

        batches = await asyncio.gather(*[_chunk(w) for w in windows])
        rows: list[dict[str, Any]] = []
        for batch in batches:
            rows.extend(batch)
        return rows

    async def _pmxt_trades(
        self,
        token: str,
        start: datetime,
        end: datetime,
        market: Any,
    ) -> list[dict[str, Any]]:
        try:
            trades = await self.pmxt.fetch_trades(token, limit=1000, start=start, end=end)
        except Exception as exc:
            logger.debug("PMXT trades {} : {}", market.slug, exc)
            return []
        rows = []
        for t in trades:
            if not t.get("timestamp"):
                continue
            rows.append(
                {
                    "record_type": "trade",
                    "timestamp": t["timestamp"],
                    "market_id": market.market_id,
                    "slug": market.slug,
                    "trade_id": t.get("trade_id"),
                    "price": t.get("price"),
                    "size": t.get("size"),
                    "side": t.get("side"),
                }
            )
        return rows

    async def _pmxt_ohlcv(
        self,
        token: str,
        start: datetime,
        end: datetime,
        market: Any,
    ) -> list[dict[str, Any]]:
        try:
            candles = await self.pmxt.fetch_ohlcv(
                token, resolution="1m", start=start, end=end, limit=1000
            )
        except Exception as exc:
            logger.debug("PMXT ohlcv {} : {}", market.slug, exc)
            return []
        rows = []
        for c in candles:
            ts = c.get("timestamp")
            if ts is None:
                continue
            dt = ms_to_datetime(ts) if float(ts) > 1e12 else datetime.fromtimestamp(float(ts), tz=UTC)
            rows.append(
                {
                    "record_type": "ohlcv",
                    "timestamp": dt,
                    "market_id": market.market_id,
                    "slug": market.slug,
                    "open": c["open"],
                    "high": c["high"],
                    "low": c["low"],
                    "close": c["close"],
                    "volume": c.get("volume") or 0,
                }
            )
        return rows

    async def _clob_price_history(
        self,
        token: str,
        start: datetime,
        end: datetime,
        market: Any,
    ) -> list[dict[str, Any]]:
        try:
            history = await self.poly.get_price_history(
                token,
                start_ts=int(start.timestamp()),
                end_ts=int(end.timestamp()),
                fidelity=1,
            )
        except Exception as exc:
            logger.debug("CLOB price history {} : {}", market.slug, exc)
            return []
        rows = []
        for point in history:
            ts = point.get("t") or point.get("timestamp")
            price = point.get("p") or point.get("price")
            if ts is None or price is None:
                continue
            dt = ms_to_datetime(ts) if float(ts) > 1e12 else datetime.fromtimestamp(float(ts), tz=UTC)
            rows.append(
                {
                    "record_type": "trade",
                    "timestamp": dt,
                    "market_id": market.market_id,
                    "slug": market.slug,
                    "price": float(price),
                    "size": 0.0,
                    "side": "hist",
                }
            )
        return rows

    async def _btc_bars(self, start: datetime, end: datetime, market: Any) -> list[dict[str, Any]]:
        # Build request windows and fetch in parallel
        interval = "1s"
        step = timedelta(seconds=1000)  # 1000 bars of 1s
        windows: list[tuple[datetime, datetime]] = []
        cursor = start
        while cursor < end:
            until = min(cursor + step, end)
            windows.append((cursor, until))
            cursor = until

        async def _fetch(window: tuple[datetime, datetime], use_interval: str) -> tuple[str, list[list[Any]]]:
            since, until = window
            try:
                klines = await self.binance.get_klines(
                    interval=use_interval,
                    start_time=since,
                    end_time=until,
                    limit=1000,
                )
                return use_interval, klines
            except Exception as exc:
                if use_interval == "1s":
                    logger.debug("1s kline fail {}, retry 1m", exc)
                    try:
                        klines = await self.binance.get_klines(
                            interval="1m",
                            start_time=since,
                            end_time=until,
                            limit=1000,
                        )
                        return "1m", klines
                    except Exception as exc2:
                        logger.debug("BTC bars failed: {}", exc2)
                        return "1m", []
                logger.debug("BTC bars failed: {}", exc)
                return use_interval, []

        results = await asyncio.gather(*[_fetch(w, interval) for w in windows])
        rows: list[dict[str, Any]] = []
        for used_interval, klines in results:
            for k in klines:
                ts = ms_to_datetime(k[0])
                if ts > end:
                    continue
                rows.append(
                    {
                        "record_type": "btc_1s",
                        "timestamp": ts,
                        "market_id": market.market_id,
                        "slug": market.slug,
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "trade_count": int(k[8]) if len(k) > 8 else None,
                        "interval": used_interval,
                    }
                )
        return rows
