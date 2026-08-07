"""Buffered ZSTD parquet writers with periodic flush."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
import pyarrow.parquet as pq

from app.config import settings
from app.schemas import SCHEMAS, TABLE_FILES
from loguru import logger


Deduper = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


def _default_dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = tuple(sorted(row.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def dedupe_by_timestamp(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep last row per floored-second timestamp (1s snapshots)."""
    by_ts: dict[int, dict[str, Any]] = {}
    for row in rows:
        ts = int(row["timestamp"])
        by_ts[ts] = row
    return [by_ts[k] for k in sorted(by_ts)]


class ParquetBuffer:
    def __init__(
        self,
        path: Path,
        table: str,
        *,
        dedupe: Deduper | None = None,
    ) -> None:
        if table not in SCHEMAS:
            raise KeyError(table)
        self.path = path
        self.table = table
        self.schema = SCHEMAS[table]
        self.dedupe = dedupe or _default_dedupe
        self._rows: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._dirty = False
        self._since_flush = 0

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._rows)

    @property
    def since_flush(self) -> int:
        with self._lock:
            return self._since_flush

    def append(self, row: dict[str, Any]) -> int:
        with self._lock:
            self._rows.append(row)
            self._dirty = True
            self._since_flush += 1
            return self._since_flush

    def extend(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            with self._lock:
                return self._since_flush
        with self._lock:
            self._rows.extend(rows)
            self._dirty = True
            self._since_flush += len(rows)
            return self._since_flush

    def flush(self, *, force: bool = False) -> int:
        with self._lock:
            if not self._dirty and not force:
                return 0
            if not self._rows and not self.path.is_file():
                self._dirty = False
                self._since_flush = 0
                return 0
            rows = self.dedupe(list(self._rows))
            self._rows = rows
            n = len(rows)
            if n == 0:
                self._dirty = False
                self._since_flush = 0
                return 0
            table = pa.Table.from_pylist(rows, schema=self.schema)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(
                table,
                self.path,
                compression=settings.parquet_compression,
            )
            self._dirty = False
            self._since_flush = 0
            return n


def table_path(market_dir: Path, table: str) -> Path:
    return market_dir / TABLE_FILES[table]
