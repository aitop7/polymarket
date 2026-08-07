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
            "btc_trades": ParquetBuffer(table_path(self.dir, "btc_trades"), "btc_trades"),
            "btc_depth": ParquetBuffer(
                table_path(self.dir, "btc_depth"),
                "btc_depth",
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
            "btc_trades",
            {
                "timestamp": int(timestamp),
                "price": float(price),
                "quantity": float(quantity),
                "buyer_is_maker": bool(buyer_is_maker),
            },
        )

    def append_depth(self, row: dict[str, Any]) -> None:
        ts = int(row["timestamp"])
        if not self.in_window(ts):
            return
        self.append("btc_depth", row)

    def append_orderbook(self, row: dict[str, Any]) -> None:
        ts = int(row["timestamp"])
        if not self.in_window(ts):
            return
        self.append("orderbooks", row)

    def append_pm_trade(self, row: dict[str, Any]) -> None:
        ts = int(row["timestamp"])
        if not self.in_window(ts):
            return
        self.append("trades", row)

    def pending_rows(self) -> int:
        return sum(b.pending for b in self.buffers.values())

    def rows_since_flush(self) -> int:
        return sum(b.since_flush for b in self.buffers.values())

    def flush(self, *, force: bool = False) -> None:
        for name, buf in self.buffers.items():
            n = buf.flush(force=force)
            if n:
                logger.debug("Flushed {} rows → {}/{}", n, self.dir.name, TABLE_FILES[name])

    def should_flush(self) -> bool:
        return self.rows_since_flush() >= settings.flush_max_rows
