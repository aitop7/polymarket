from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from app.api.binance import BinanceClient
from app.api.pmxt_client import PmxtClient
from app.api.polymarket import PolymarketClient
from app.config import settings
from app.features import FeatureEngine
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
from app.utils.time import utcnow


class HistorySynchronizer:
    """
    Backfill analysis-grade multi-table bundles under by_market/{slug}/.
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
        written = 0
        lock = asyncio.Lock()
        progress = ProgressBar(total, prefix="Download")
        progress.update(0, current="starting...", written=0)

        async def _one(market: Any) -> bool:
            nonlocal done, written
            async with self._market_sem:
                slug = str(market.slug or market.market_id)
                async with lock:
                    progress.update(done, current=f"fetching {slug}", written=written)
                try:
                    ok = await self._sync_one_market(market)
                    async with lock:
                        done += 1
                        if ok:
                            written += 1
                        progress.update(done, current=slug, written=written)
                    return ok
                except Exception as exc:
                    async with lock:
                        done += 1
                        progress.update(done, current=slug, written=written)
                    logger.warning("History sync failed for {}: {}", market.market_id, exc)
                    return False

        try:
            await asyncio.gather(*[_one(m) for m in all_markets])
        finally:
            pct = 100.0 * done / total if total else 100.0
            progress.close(
                final_msg=f"Download complete: {written}/{total} market bundles ({pct:.1f}%)"
            )
        return written

    async def _sync_one_market(self, market: Any) -> bool:
        start, end = resolve_market_window(market, lookback_days=self.lookback_days)
        settlement = market.settlement_time or market.end_time or end
        slug = str(market.slug or market.market_id)

        btc_ticks, trades_raw, open_px, close_px = await asyncio.gather(
            self._btc_ticks(start, end),
            self._fetch_trades(market, start, end),
            self._btc_price_at(start),
            self._btc_price_at(end),
        )

        trade_rows, book_rows, wallet_rows, feature_rows = self._build_pm_tables(
            trades_raw,
            market=market,
            start=start,
            end=end,
            settlement=settlement,
        )

        meta = {
            **market.as_dict(),
            "slug": slug,
            "start_time": start,
            "end_time": end,
            "settlement_time": settlement,
            "opening_btc_price": open_px,
            "closing_btc_price": close_px,
        }
        paths = sessions.write_market_bundle(
            market.market_id,
            slug,
            meta=meta,
            tables={
                "btc_ticks": btc_ticks,
                "trades": trade_rows,
                "orderbooks": book_rows,
                "wallet_positions": wallet_rows,
                "features": feature_rows,
                "orders": [],
            },
        )
        return bool(paths)

    async def _btc_price_at(self, when: datetime) -> float | None:
        try:
            klines = await self.binance.get_klines(
                interval="1s",
                start_time=when,
                end_time=when + timedelta(seconds=2),
                limit=1,
            )
            if klines:
                return float(klines[0][4])
        except Exception:
            pass
        try:
            klines = await self.binance.get_klines(
                interval="1m",
                start_time=when - timedelta(minutes=1),
                end_time=when + timedelta(minutes=1),
                limit=1,
            )
            if klines:
                return float(klines[0][4])
        except Exception as exc:
            logger.debug("BTC price at {} failed: {}", when, exc)
        return None

    async def _btc_ticks(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        try:
            raw = await self.binance.iter_agg_trades(start_time=start, end_time=end)
        except Exception as exc:
            logger.debug("BTC ticks failed: {}", exc)
            return []
        rows = []
        for t in raw:
            rows.append(BinanceClient.normalize_agg_trade(t))
        return rows

    async def _fetch_trades(self, market: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
        condition_id = getattr(market, "condition_id", None)
        if not condition_id:
            return []
        try:
            return await self.poly.get_trades(
                str(condition_id),
                start_ts=int(start.timestamp()),
                end_ts=int(end.timestamp()),
            )
        except Exception as exc:
            logger.debug("data-api trades {} : {}", market.slug, exc)
            return []

    def _build_pm_tables(
        self,
        trades: list[dict[str, Any]],
        *,
        market: Any,
        start: datetime,
        end: datetime,
        settlement: datetime,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        from app.config import const
        from app.features.depth_bands import build_orderbook_row, levels_from_prints

        trade_rows: list[dict[str, Any]] = []
        # per-second prints by outcome: buys -> asks, sells -> bids
        by_sec: dict[datetime, dict[str, dict[str, list[tuple[float, float]]]]] = defaultdict(
            lambda: {
                "up": {"buys": [], "sells": [], "all": []},
                "down": {"buys": [], "sells": [], "all": []},
            }
        )
        wallet_state: dict[str, dict[str, float]] = {}
        wallet_rows: list[dict[str, Any]] = []
        token_yes = getattr(market, "token_yes", None)
        token_no = getattr(market, "token_no", None)

        def _outcome_key(outcome: str, asset: Any) -> str | None:
            oc = (outcome or "").lower()
            if oc in {"yes", "up"}:
                return "up"
            if oc in {"no", "down"}:
                return "down"
            if token_yes and str(asset) == str(token_yes):
                return "up"
            if token_no and str(asset) == str(token_no):
                return "down"
            return None

        ordered = sorted(trades, key=lambda t: int(t.get("timestamp") or 0))
        for trade in ordered:
            ts_raw = trade.get("timestamp")
            if ts_raw is None:
                continue
            ts = datetime.fromtimestamp(int(ts_raw), tz=UTC)
            if ts < start or ts > end:
                continue
            try:
                price = float(trade["price"])
                size = float(trade.get("size") or 0)
            except (TypeError, ValueError, KeyError):
                continue
            side = str(trade.get("side") or "").lower()
            outcome = str(trade.get("outcome") or "")
            wallet = trade.get("proxyWallet") or trade.get("wallet")
            tx = trade.get("transactionHash") or trade.get("tx")
            asset = trade.get("asset")
            trade_rows.append(
                {
                    "timestamp": ts,
                    "trade_id": str(tx or f"{asset}-{int(ts_raw)}-{price}-{size}"),
                    "price": price,
                    "size": size,
                    "side": side,
                    "wallet": wallet,
                    "outcome": outcome,
                    "tx": tx,
                    "asset": asset,
                }
            )

            key = _outcome_key(outcome, asset)
            sec = ts.replace(microsecond=0)
            if key:
                by_sec[sec][key]["all"].append((price, size))
                if side == "buy":
                    by_sec[sec][key]["buys"].append((price, size))
                elif side == "sell":
                    by_sec[sec][key]["sells"].append((price, size))

            if wallet:
                state = wallet_state.setdefault(
                    str(wallet), {"yes_position": 0.0, "no_position": 0.0, "pnl": 0.0}
                )
                oc = outcome.lower()
                if oc in {"yes", "up"}:
                    state["yes_position"] += size if side == "buy" else -size
                elif oc in {"no", "down"}:
                    state["no_position"] += size if side == "buy" else -size
                elif side == "buy":
                    state["yes_position"] += size
                else:
                    state["yes_position"] -= size
                wallet_rows.append(
                    {
                        "timestamp": ts,
                        "wallet": str(wallet),
                        "yes_position": state["yes_position"],
                        "no_position": state["no_position"],
                        "pnl": state["pnl"],
                    }
                )

        features = FeatureEngine()
        feature_rows: list[dict[str, Any]] = []
        book_rows: list[dict[str, Any]] = []

        window_s = const.DEPTH_BAND_WINDOW_S
        windows: dict[str, dict[str, list[tuple[datetime, float, float]]]] = {
            "up": {"buys": [], "sells": []},
            "down": {"buys": [], "sells": []},
        }
        last_prices: dict[str, float | None] = {"up": None, "down": None}
        last_row: dict[str, Any] | None = None

        cursor = start.replace(microsecond=0)
        end_sec = end.replace(microsecond=0)
        while cursor <= end_sec:
            bucket = by_sec.get(cursor)
            if bucket:
                for key in ("up", "down"):
                    for price, size in bucket[key]["sells"]:
                        windows[key]["sells"].append((cursor, price, size))
                    for price, size in bucket[key]["buys"]:
                        windows[key]["buys"].append((cursor, price, size))
                    for price, size in bucket[key]["all"]:
                        features.note_trade(f"{market.market_id}:{key}", size, price)
                        last_prices[key] = price

            cutoff = cursor - timedelta(seconds=window_s)
            for key in ("up", "down"):
                windows[key]["sells"] = [x for x in windows[key]["sells"] if x[0] >= cutoff]
                windows[key]["buys"] = [x for x in windows[key]["buys"] if x[0] >= cutoff]

            up_bids = levels_from_prints(
                [(p, s) for _, p, s in windows["up"]["sells"]], reverse=True
            )
            up_asks = levels_from_prints(
                [(p, s) for _, p, s in windows["up"]["buys"]], reverse=False
            )
            down_bids = levels_from_prints(
                [(p, s) for _, p, s in windows["down"]["sells"]], reverse=True
            )
            down_asks = levels_from_prints(
                [(p, s) for _, p, s in windows["down"]["buys"]], reverse=False
            )

            has_book = bool(up_bids or up_asks or down_bids or down_asks)
            if has_book:
                last_row = build_orderbook_row(
                    timestamp=cursor,
                    up_bids=up_bids,
                    up_asks=up_asks,
                    down_bids=down_bids,
                    down_asks=down_asks,
                    up_price=last_prices["up"],
                    down_price=last_prices["down"],
                )
                book_rows.append(last_row)
            elif last_row is not None:
                fwd = dict(last_row)
                from app.features.depth_bands import timestamp_to_ms

                fwd["timestamp"] = timestamp_to_ms(cursor)
                book_rows.append(fwd)

            book_for_feat = {"bids": up_bids, "asks": up_asks}
            if up_bids or up_asks:
                feat = features.compute(
                    market_id=market.market_id,
                    book=book_for_feat,
                    settlement_time=settlement,
                    timestamp=cursor,
                )
                feature_rows.append(
                    {
                        "timestamp": cursor,
                        "spread": feat.get("spread"),
                        "imbalance": feat.get("imbalance"),
                        "momentum": feat.get("momentum"),
                        "volatility": feat.get("volatility"),
                        "depth": feat.get("depth"),
                        "whale_score": feat.get("whale_score"),
                        "time_remaining": max(0.0, (settlement - cursor).total_seconds()),
                    }
                )
            else:
                feature_rows.append(
                    {
                        "timestamp": cursor,
                        "spread": None,
                        "imbalance": None,
                        "momentum": None,
                        "volatility": None,
                        "depth": None,
                        "whale_score": 0.0,
                        "time_remaining": max(0.0, (settlement - cursor).total_seconds()),
                    }
                )
            cursor += timedelta(seconds=1)

        return trade_rows, book_rows, wallet_rows, feature_rows
