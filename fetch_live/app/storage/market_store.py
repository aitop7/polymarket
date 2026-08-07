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
            "trades": ParquetBuffer(table_path(self.dir, "trades"), "trades"),
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
        """Insert/update Polymarket fill by transaction hash (RTDS / Data API)."""
        ts = int(row["timestamp"])
        # Same conditionId may print before official open; reject only after end
        # or more than one window early.
        if ts >= self.end_ms:
            return
        if ts < self.start_ms - 300_000:
            return
        key = (tx_hash or str(row.get("transaction_hash") or "")).strip().lower()
        if not key:
            key = (
                f"notx:{ts}:{row.get('token')}:{row.get('side')}:"
                f"{row.get('price')}:{row.get('shares')}"
            )
        with self._lock:
            prev = self._pm_trades.get(key)
            if prev is None:
                self._pm_trades[key] = {
                    "timestamp": ts,
                    "wallet": str(row.get("wallet") or ""),
                    "token": bool(row.get("token")),
                    "side": bool(row.get("side")),
                    "price": float(row["price"]),
                    "shares": int(row["shares"]),
                }
                self._pm_trades_dirty = True
                self.buffers["trades"]._since_flush += 1
                return
            changed = False
            wallet = str(row.get("wallet") or "")
            if wallet and wallet != prev.get("wallet"):
                prev["wallet"] = wallet
                changed = True
            # Prefer latest API fields for this tx
            if int(prev["timestamp"]) != ts:
                prev["timestamp"] = ts
                changed = True
            price = float(row["price"])
            shares = int(row["shares"])
            token = bool(row.get("token"))
            side = bool(row.get("side"))
            if prev["price"] != price or prev["shares"] != shares:
                prev["price"] = price
                prev["shares"] = shares
                changed = True
            if prev["token"] != token or prev["side"] != side:
                prev["token"] = token
                prev["side"] = side
                changed = True
            if changed:
                self._pm_trades_dirty = True

    def upsert_pm_trades(self, rows: list[dict[str, Any]]) -> int:
        before = len(self._pm_trades)
        for row in rows:
            self.upsert_pm_trade(row)
        return max(0, len(self._pm_trades) - before)

    def wallet_fill_rate(self) -> tuple[int, int]:
        with self._lock:
            n = len(self._pm_trades)
            filled = sum(1 for r in self._pm_trades.values() if r.get("wallet"))
            return filled, n

    def _sync_pm_trades_buffer(self) -> None:
        with self._lock:
            if not self._pm_trades_dirty:
                return
            rows = sorted(self._pm_trades.values(), key=lambda r: int(r["timestamp"]))
            self._pm_trades_dirty = False
        buf = self.buffers["trades"]
        with buf._lock:
            buf._rows = [
                {
                    "timestamp": int(r["timestamp"]),
                    "wallet": str(r.get("wallet") or ""),
                    "token": bool(r["token"]),
                    "side": bool(r["side"]),
                    "price": float(r["price"]),
                    "shares": int(r["shares"]),
                }
                for r in rows
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
