from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import settings
from app.utils.logger import logger
from app.utils.time import utcnow


class ParquetStore:
    """
    Append-only compressed Parquet writer, one directory per collector.

    Layout:
      {data_dir}/{collector}/YYYY/MM/DD/{collector}_{HHMMSS_ffffff}.parquet
    """

    def __init__(
        self,
        data_dir: Path | None = None,
        compression: str | None = None,
    ) -> None:
        self.data_dir = Path(data_dir or settings.data_dir)
        self.compression = compression or settings.parquet_compression
        self._locks: dict[str, threading.Lock] = {}
        self._global = threading.Lock()

    def _lock_for(self, collector: str) -> threading.Lock:
        with self._global:
            if collector not in self._locks:
                self._locks[collector] = threading.Lock()
            return self._locks[collector]

    def collector_dir(self, collector: str, ts: datetime | None = None) -> Path:
        when = ts or utcnow()
        path = (
            self.data_dir
            / collector
            / f"{when.year:04d}"
            / f"{when.month:02d}"
            / f"{when.day:02d}"
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write(self, collector: str, rows: list[dict[str, Any]], ts: datetime | None = None) -> Path | None:
        if not rows:
            return None

        when = ts or utcnow()
        df = self._normalize_df(pd.DataFrame(rows))
        out_dir = self.collector_dir(collector, when)
        file_path = out_dir / f"{collector}_{when.strftime('%H%M%S_%f')}.parquet"

        with self._lock_for(collector):
            df.to_parquet(file_path, index=False, compression=self.compression)

        logger.debug(
            "Parquet {} rows={} bytes={} path={}",
            collector,
            len(df),
            file_path.stat().st_size,
            file_path,
        )
        return file_path

    def write_snapshot(self, name: str, rows: list[dict[str, Any]]) -> Path | None:
        """Overwrite a latest snapshot (e.g. active markets registry)."""
        if not rows:
            return None
        snap_dir = self.data_dir / "_snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        path = snap_dir / f"{name}.parquet"
        df = self._normalize_df(pd.DataFrame(rows))
        with self._lock_for(f"snap:{name}"):
            df.to_parquet(path, index=False, compression=self.compression)
        return path

    def read_snapshot(self, name: str) -> list[dict[str, Any]]:
        path = self.data_dir / "_snapshots" / f"{name}.parquet"
        if not path.exists():
            return []
        df = pd.read_parquet(path)
        return df.to_dict(orient="records")

    def load_checkpoint(self, source: str) -> dict[str, Any] | None:
        data = self._read_checkpoints()
        return data.get(source)

    def save_checkpoint(
        self,
        source: str,
        *,
        cursor: str | None = None,
        last_timestamp: datetime | None = None,
    ) -> None:
        data = self._read_checkpoints()
        data[source] = {
            "cursor": cursor,
            "last_timestamp": last_timestamp.isoformat() if last_timestamp else None,
            "updated_at": utcnow().isoformat(),
        }
        path = self.data_dir / "_checkpoints.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _read_checkpoints(self) -> dict[str, Any]:
        path = self.data_dir / "_checkpoints.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        for col in out.columns:
            if pd.api.types.is_datetime64_any_dtype(out[col]):
                out[col] = pd.to_datetime(out[col], utc=True)
                continue
            if out[col].dtype != object:
                continue
            sample = out[col].dropna().head(1)
            if not len(sample):
                continue
            val = sample.iloc[0]
            if isinstance(val, datetime):
                out[col] = pd.to_datetime(out[col], utc=True)
            elif isinstance(val, (dict, list)):
                out[col] = out[col].map(
                    lambda x: json.dumps(x, default=str) if x is not None else None
                )
            elif hasattr(val, "isoformat"):
                out[col] = out[col].astype(str)
        return out


# Shared singleton used by collectors
store = ParquetStore()
