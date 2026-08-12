"""Per-market directory store: meta.json + parquet buffers."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from app.config import settings
from app.schemas import TABLE_FILES
from app.storage.parquet_buffer import ParquetBuffer, dedupe_by_timestamp, table_path
from app.trades_mode import get_trades_mode


def safe_name(value: str) -> str:
    text = (value or "unknown").strip()
    text = re.sub(r"[^\w.\-]+", "_", text)
    return text[:180] or "unknown"


def market_date_key(start_ms: int) -> str:
    dt = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


class MarketStore:
    """One active (or recently closed) market's on-disk + buffered writers."""

    def __init__(self, meta: dict[str, Any]) -> None:
        self.meta = dict(meta)
        self.market_id = str(meta["market_id"])
        start_ms = int(meta["start_time"])
        self.start_ms = start_ms
        self.end_ms = int(meta["end_time"])
        date_key = market_date_key(start_ms)
        self.dir = settings.data_dir / date_key / safe_name(self.market_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.dir / "meta.json"

        self.buffers: dict[str, ParquetBuffer] = {
            "binance_trades": ParquetBuffer(
                table_path(self.dir, "binance_trades"), "binance_trades"
            ),
            "binance_price_orderbook": ParquetBuffer(
                table_path(self.dir, "binance_price_orderbook"),
                "binance_price_orderbook",
                dedupe=dedupe_by_timestamp,
            ),
            "chainlink_price": ParquetBuffer(
                table_path(self.dir, "chainlink_price"),
                "chainlink_price",
                dedupe=dedupe_by_timestamp,
            ),
            "orderbooks": ParquetBuffer(
                table_path(self.dir, "orderbooks"),
                "orderbooks",
                dedupe=dedupe_by_timestamp,
            ),
            "trades": ParquetBuffer(
                table_path(self.dir, "trades"),
                "trades",
                dedupe=lambda rows: rows,  # never collapse identical Orbscan fills
            ),
        }
        # Internal dedupe keys not written to parquet
        self._seen_agg_ids: set[int] = set()
        # Polymarket trades keyed by transaction hash (Data API).
        self._pm_trades: dict[str, dict[str, Any]] = {}
        self._pm_trades_dirty = False
        self._lock = threading.Lock()
        self.write_meta()
        logger.info("Market store ready {}", self.dir)

    def write_meta(self) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")
        tmp.replace(self.meta_path)

    def update_meta(self, **fields: Any) -> None:
        with self._lock:
            self.meta.update(fields)
            self.write_meta()

    def in_window(self, ts_ms: int) -> bool:
        return self.start_ms <= int(ts_ms) < self.end_ms

    def append(self, table: str, row: dict[str, Any]) -> int:
        buf = self.buffers[table]
        return buf.append(row)

    def try_btc_trade(
        self,
        *,
        agg_id: int,
        timestamp: int,
        price: float,
        quantity: float,
        buyer_is_maker: bool,
    ) -> None:
        if not self.in_window(timestamp):
            return
        if agg_id in self._seen_agg_ids:
            return
        self._seen_agg_ids.add(agg_id)
        # Bound memory: keep recent ids only
        if len(self._seen_agg_ids) > 200_000:
            self._seen_agg_ids = set(list(self._seen_agg_ids)[-100_000:])
        self.append(
            "binance_trades",
            {
                "timestamp": int(timestamp),
                "price": float(price),
                "quantity": float(quantity),
                "buyer_is_maker": bool(buyer_is_maker),
            },
        )

    def append_binance_price_orderbook(self, row: dict[str, Any]) -> None:
        ts = int(row["timestamp"])
        if not self.in_window(ts):
            return
        self.append("binance_price_orderbook", row)

    def append_chainlink_price(self, row: dict[str, Any]) -> None:
        ts = int(row["timestamp"])
        if not self.in_window(ts):
            return
        self.append("chainlink_price", row)

    def append_orderbook(self, row: dict[str, Any]) -> None:
        ts = int(row["timestamp"])
        if not self.in_window(ts):
            return
        self.append("orderbooks", row)

    def upsert_pm_trade(self, row: dict[str, Any], *, tx_hash: str = "") -> None:
        """Insert/replace one fill row (Orbscan: same wallet may have many rows)."""
        ts = int(row["timestamp"])
        if ts >= self.end_ms:
            return
        if ts < self.start_ms - 300_000:
            return
        tx = (tx_hash or str(row.get("transaction_hash") or "")).strip()
        wallet = str(row.get("wallet") or "").strip().lower()
        is_up = bool(row.get("is_up"))
        is_buy = bool(row.get("is_buy"))
        is_taker = bool(row.get("is_taker", True))
        if get_trades_mode() == "taker" and not is_taker:
            return
        try:
            price = round(float(row["price"]), 6)
        except (TypeError, ValueError):
            price = 0.0
        try:
            shares = round(max(0.0, float(row["shares"])), 2)
        except (TypeError, ValueError):
            shares = 0.0
        key = str(row.get("fill_key") or "").strip()
        if not key:
            from app.polymarket.data_api_trades import assign_fill_keys

            keyed = assign_fill_keys(
                [
                    {
                        "timestamp": ts,
                        "transaction_hash": tx,
                        "wallet": wallet,
                        "is_up": is_up,
                        "is_buy": is_buy,
                        "is_taker": is_taker,
                        "price": price,
                        "shares": shares,
                    }
                ]
            )
            key = str(keyed[0].get("fill_key") or "")
        with self._lock:
            prev = self._pm_trades.get(key)
            if prev is None:
                self._pm_trades[key] = {
                    "timestamp": ts,
                    "transaction_hash": tx,
                    "wallet": wallet,
                    "is_up": is_up,
                    "is_buy": is_buy,
                    "is_taker": is_taker,
                    "price": price,
                    "shares": shares,
                    "fill_index": int(row.get("fill_index") or 0),
                    "fill_key": key,
                }
                self._pm_trades_dirty = True
                self.buffers["trades"]._since_flush += 1
                return
            changed = False
            if str(prev.get("wallet") or "") != wallet:
                prev["wallet"] = wallet
                changed = True
            if int(prev["timestamp"]) != ts:
                prev["timestamp"] = ts
                changed = True
            if abs(float(prev.get("price") or 0) - price) > 1e-12:
                prev["price"] = price
                changed = True
            if abs(float(prev.get("shares") or 0) - shares) > 1e-12:
                prev["shares"] = shares
                changed = True
            prev_taker = bool(prev.get("is_taker", True))
            if prev_taker and not is_taker:
                prev["is_taker"] = False
                prev["is_up"] = is_up
                prev["is_buy"] = is_buy
                changed = True
            elif not prev_taker and is_taker:
                pass
            elif (
                bool(prev.get("is_up")) != is_up
                or bool(prev.get("is_buy")) != is_buy
                or prev_taker != is_taker
            ):
                prev["is_up"] = is_up
                prev["is_buy"] = is_buy
                prev["is_taker"] = is_taker
                changed = True
            if changed:
                self._pm_trades_dirty = True

    def upsert_pm_trades(self, rows: list[dict[str, Any]]) -> int:
        from app.polymarket.data_api_trades import _fill_base_key, normalize_trade_legs

        rows = normalize_trade_legs(rows)
        if not rows:
            return 0

        from collections import Counter, defaultdict

        def _merge_tx(
            old: list[dict[str, Any]], new: list[dict[str, Any]]
        ) -> list[dict[str, Any]]:
            """
            Merge Orbscan legs for one tx.

            - Prefer the richer multiset per fill base-key (never drop a real
              identical duplicate just because a later poll omitted it).
            - If old is an exact uniform N× inflation of new (N>=2), take new
              (heal all_raw+taker_raw double-count).
            - If key sets match, new has real duplicate legs, and old is larger,
              take new (old was inflated uniques; new restored true dups).
            """
            if not new:
                return list(old)
            if not old:
                return list(new)
            oc = Counter(_fill_base_key(r) for r in old)
            nc = Counter(_fill_base_key(r) for r in new)
            if oc == nc:
                return list(new)
            # Exact uniform N× inflation (N>=2) of the incoming multiset.
            if set(oc) == set(nc) and nc:
                ratios = {oc[k] // nc[k] for k in nc if nc[k] > 0 and oc[k] % nc[k] == 0}
                if len(ratios) == 1 and next(iter(ratios)) >= 2:
                    return list(new)
            # Same keys, incoming restored true duplicate legs, old was larger
            # (typically 2× unique-only pollution) → trust incoming.
            if (
                set(oc) == set(nc)
                and any(v >= 2 for v in nc.values())
                and len(new) < len(old)
            ):
                return list(new)
            if len(new) >= len(old) and all(nc[k] >= oc.get(k, 0) for k in nc):
                return list(new)

            # Max-merge counts per base key; prefer payloads from the side that
            # already has enough copies.
            old_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
            new_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for r in old:
                old_by[_fill_base_key(r)].append(r)
            for r in new:
                new_by[_fill_base_key(r)].append(r)
            merged: list[dict[str, Any]] = []
            for key in set(old_by) | set(new_by):
                o_list = old_by.get(key, [])
                n_list = new_by.get(key, [])
                need = max(len(o_list), len(n_list))
                primary = n_list if len(n_list) >= len(o_list) else o_list
                secondary = o_list if primary is n_list else n_list
                chosen = list(primary[:need])
                if len(chosen) < need:
                    chosen.extend(secondary[len(chosen) : need])
                merged.extend(chosen)
            return normalize_trade_legs(merged)

        incoming_by_tx: dict[str, list[dict[str, Any]]] = {}
        no_tx_rows: list[dict[str, Any]] = []
        for r in rows:
            tx = str(r.get("transaction_hash") or "").strip().lower()
            if not tx:
                no_tx_rows.append(r)
                continue
            incoming_by_tx.setdefault(tx, []).append(r)

        before = len(self._pm_trades)
        merged_by_tx: dict[str, list[dict[str, Any]]] = {}
        with self._lock:
            for tx, new_rows in incoming_by_tx.items():
                old_rows = [
                    r
                    for r in self._pm_trades.values()
                    if str(r.get("transaction_hash") or "").strip().lower() == tx
                ]
                merged_by_tx[tx] = _merge_tx(old_rows, new_rows)
                drop_keys = [
                    k
                    for k, r in self._pm_trades.items()
                    if str(r.get("transaction_hash") or "").strip().lower() == tx
                ]
                for k in drop_keys:
                    del self._pm_trades[k]
                self._pm_trades_dirty = True

        for row in no_tx_rows:
            self.upsert_pm_trade(row)
        for tx, merged_rows in merged_by_tx.items():
            for row in merged_rows:
                self.upsert_pm_trade(row)
        return max(0, len(self._pm_trades) - before)

    def wallet_fill_rate(self) -> tuple[int, int]:
        with self._lock:
            n = len(self._pm_trades)
            filled = sum(1 for r in self._pm_trades.values() if r.get("wallet"))
            return filled, n

    def _sync_pm_trades_buffer(self) -> None:
        from app.polymarket.data_api_trades import normalize_trade_legs

        with self._lock:
            raw_rows = list(self._pm_trades.values())
            if get_trades_mode() == "taker":
                takers = [r for r in raw_rows if bool(r.get("is_taker", True))]
                if len(takers) != len(raw_rows):
                    self._pm_trades_dirty = True
                raw_rows = takers
            if not self._pm_trades_dirty:
                return
            rows = normalize_trade_legs(raw_rows)
            rebuilt: dict[str, dict[str, Any]] = {}
            for r in rows:
                key = str(r.get("fill_key") or "").strip()
                if not key:
                    continue
                rebuilt[key] = r
            self._pm_trades = rebuilt
            self._pm_trades_dirty = False
            sorted_rows = sorted(rebuilt.values(), key=lambda r: int(r["timestamp"]))
        buf = self.buffers["trades"]
        with buf._lock:
            buf._rows = [
                {
                    "timestamp": int(r["timestamp"]),
                    "transaction_hash": str(r.get("transaction_hash") or ""),
                    "wallet": str(r.get("wallet") or ""),
                    "is_up": bool(r.get("is_up")),
                    "is_buy": bool(r.get("is_buy")),
                    "is_taker": bool(r.get("is_taker", True)),
                    "price": round(float(r["price"]), 6),
                    "shares": round(float(r["shares"]), 2),
                    "fill_index": int(r.get("fill_index") or 0),
                }
                for r in sorted_rows
            ]
            buf._dirty = True

    def pending_rows(self) -> int:
        return sum(b.pending for b in self.buffers.values())

    def rows_since_flush(self) -> int:
        return sum(b.since_flush for b in self.buffers.values())

    def flush(self, *, force: bool = False) -> None:
        self._sync_pm_trades_buffer()
        for name, buf in self.buffers.items():
            n = buf.flush(force=force)
            if n:
                logger.debug("Flushed {} rows → {}/{}", n, self.dir.name, TABLE_FILES[name])

    def should_flush(self) -> bool:
        return self.rows_since_flush() >= settings.flush_max_rows
