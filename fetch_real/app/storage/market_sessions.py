from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import settings
from app.utils.logger import logger
from app.utils.time import utcnow


COLUMNS = [
    "timestamp",
    "market_id",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trade_count",
    "buy_volume",
    "sell_volume",
    "best_bid",
    "best_ask",
    "spread",
    "imbalance",
    "momentum",
    "volatility",
    "depth",
    "whale_score",
    "time_remaining",
    "trade_price",
    "trade_size",
    "trade_side",
]


def safe_name(value: str) -> str:
    text = (value or "unknown").strip()
    text = re.sub(r"[^\w.\-]+", "_", text)
    return text[:180] or "unknown"


def _floor_second(ts: Any) -> datetime:
    if ts is None:
        ts = utcnow()
    if not isinstance(ts, datetime):
        ts = pd.to_datetime(ts, utc=True).to_pydatetime()
    if ts.tzinfo is None:
        from datetime import UTC

        ts = ts.replace(tzinfo=UTC)
    return ts.replace(microsecond=0)


class MarketSessionStore:
    """
    Flat 1-second rows, one compressed parquet per UTC day:

      {data_dir}/by_day/YYYY-MM-DD.parquet

    Each row includes market_id (many markets share one daily file).
    """

    def __init__(self, data_dir: Path | None = None, compression: str | None = None) -> None:
        self.data_dir = Path(data_dir or settings.data_dir)
        self.compression = compression or settings.parquet_compression
        # market_id -> second -> flat row (realtime)
        self._seconds: dict[str, dict[datetime, dict[str, Any]]] = defaultdict(dict)
        self._meta: dict[str, dict[str, Any]] = {}
        self._done_markets: set[str] = set()
        # history batch: day_key -> flat rows (write once at flush)
        self._history_buffer: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._history_pending: set[str] = set()
        self._lock = threading.Lock()
        self._day_locks: dict[str, threading.Lock] = {}
        self._load_done()

    @property
    def day_dir(self) -> Path:
        path = self.data_dir / "by_day"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def day_path(self, day: date | datetime | str) -> Path:
        if isinstance(day, datetime):
            key = day.strftime("%Y-%m-%d")
        elif isinstance(day, date):
            key = day.isoformat()
        else:
            key = str(day)
        return self.day_dir / f"{key}.parquet"

    def _day_lock(self, day_key: str) -> threading.Lock:
        with self._lock:
            if day_key not in self._day_locks:
                self._day_locks[day_key] = threading.Lock()
            return self._day_locks[day_key]

    def _load_done(self) -> None:
        marker = self.data_dir / "_done_markets.json"
        if marker.exists():
            try:
                self._done_markets = set(json.loads(marker.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                self._done_markets = set()

    def _save_done(self) -> None:
        marker = self.data_dir / "_done_markets.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(sorted(self._done_markets), indent=2), encoding="utf-8")

    def set_market_meta(self, market: dict[str, Any]) -> None:
        market_id = str(market.get("market_id") or "")
        if not market_id:
            return
        with self._lock:
            self._meta[market_id] = {
                "market_id": market_id,
                "slug": market.get("slug"),
                "condition_id": market.get("condition_id"),
                "token_yes": market.get("token_yes"),
                "token_no": market.get("token_no"),
                "start_time": market.get("start_time"),
                "end_time": market.get("end_time"),
                "settlement_time": market.get("settlement_time"),
                "opening_btc_price": market.get("opening_btc_price"),
                "closing_btc_price": market.get("closing_btc_price"),
                "winner": market.get("winner"),
                "status": market.get("status"),
            }

    def _row(self, market_id: str, ts: datetime) -> dict[str, Any]:
        bucket = self._seconds[market_id]
        if ts not in bucket:
            bucket[ts] = {"timestamp": ts, "market_id": market_id}
        return bucket[ts]

    def append(self, market_id: str, record_type: str, row: dict[str, Any]) -> None:
        if not market_id or market_id in self._done_markets:
            return
        ts = _floor_second(row.get("timestamp"))
        with self._lock:
            target = self._row(market_id, ts)
            target["market_id"] = market_id
            if record_type in {"btc_1s", "ohlcv"}:
                for key in ("open", "high", "low", "close", "volume", "trade_count", "buy_volume", "sell_volume"):
                    if row.get(key) is not None:
                        target[key] = row[key]
                if record_type == "ohlcv" and row.get("close") is not None and target.get("trade_price") is None:
                    target["trade_price"] = row["close"]
            elif record_type == "orderbook":
                for key in ("best_bid", "best_ask", "spread"):
                    if row.get(key) is not None:
                        target[key] = row[key]
            elif record_type == "feature":
                for key in (
                    "spread",
                    "imbalance",
                    "momentum",
                    "volatility",
                    "depth",
                    "whale_score",
                    "time_remaining",
                    "best_bid",
                    "best_ask",
                ):
                    if row.get(key) is not None:
                        target[key] = row[key]
            elif record_type == "trade":
                if row.get("price") is not None:
                    target["trade_price"] = row["price"]
                if row.get("size") is not None:
                    target["trade_size"] = float(target.get("trade_size") or 0) + float(row["size"])
                if row.get("side") is not None:
                    target["trade_side"] = row["side"]
            elif record_type in {"meta", "wallet", "order"}:
                return
            else:
                for key in COLUMNS:
                    if key not in {"timestamp", "market_id"} and row.get(key) is not None:
                        target[key] = row[key]

    def append_btc_to_active(self, bar: dict[str, Any], active_markets: list[Any]) -> None:
        ts = bar.get("timestamp")
        if ts is None:
            return
        for market in active_markets:
            start = getattr(market, "start_time", None)
            end = getattr(market, "end_time", None) or getattr(market, "settlement_time", None)
            if start and ts < start:
                continue
            if end and ts > end:
                continue
            self.append(market.market_id, "btc_1s", bar)

    def _to_dataframe(self, rows: list[dict[str, Any]]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=COLUMNS)
        df = pd.DataFrame(rows)
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = None
        df = df[COLUMNS]
        return self._normalize_df(df)

    def _merge_write_day(self, day_key: str, new_df: pd.DataFrame) -> Path:
        path = self.day_path(day_key)
        lock = self._day_lock(day_key)
        with lock:
            if path.exists():
                old = pd.read_parquet(path)
                combined = pd.concat([old, new_df], ignore_index=True)
            else:
                combined = new_df
            combined = self._normalize_df(combined)
            if "timestamp" in combined.columns and "market_id" in combined.columns:
                combined = combined.drop_duplicates(subset=["market_id", "timestamp"], keep="last")
                combined = combined.sort_values(["timestamp", "market_id"]).reset_index(drop=True)
            for col in COLUMNS:
                if col not in combined.columns:
                    combined[col] = None
            combined = combined[COLUMNS]
            combined.to_parquet(path, index=False, compression=self.compression)
        return path

    def flush(self, market_id: str) -> list[Path]:
        """Flush one market's buffer into the matching daily parquet file(s)."""
        with self._lock:
            seconds = dict(self._seconds.get(market_id, {}))
        if not seconds:
            return []

        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ts, row in seconds.items():
            row = {**row, "market_id": market_id}
            by_day[ts.strftime("%Y-%m-%d")].append(row)

        paths: list[Path] = []
        for day_key, rows in by_day.items():
            df = self._to_dataframe(rows)
            path = self._merge_write_day(day_key, df)
            paths.append(path)
            logger.info(
                "Daily parquet +{} rows market={} path={}",
                len(rows),
                market_id,
                path,
            )
        return paths

    def finalize(self, market_id: str) -> list[Path]:
        if not market_id:
            return []
        paths = self.flush(market_id)
        with self._lock:
            self._done_markets.add(market_id)
            self._seconds.pop(market_id, None)
            self._save_done()
        return paths

    def flush_all_active(self) -> int:
        with self._lock:
            ids = [mid for mid in self._seconds if mid not in self._done_markets]
        n = 0
        for market_id in ids:
            if self.flush(market_id):
                n += 1
        return n

    def finalize_closed(self, closed_market_ids: list[str]) -> list[Path]:
        paths: list[Path] = []
        for market_id in closed_market_ids:
            paths.extend(self.finalize(market_id))
        return paths

    @staticmethod
    def _flatten_typed_rows(market_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse typed history records into one flat row per second."""
        seconds: dict[datetime, dict[str, Any]] = {}

        def _ensure(ts: datetime) -> dict[str, Any]:
            if ts not in seconds:
                seconds[ts] = {"timestamp": ts, "market_id": market_id}
            return seconds[ts]

        for row in rows:
            record_type = str(row.get("record_type") or "row")
            if record_type in {"meta", "wallet", "order"}:
                continue
            ts = _floor_second(row.get("timestamp"))
            target = _ensure(ts)
            target["market_id"] = market_id
            if record_type in {"btc_1s", "ohlcv"}:
                for key in ("open", "high", "low", "close", "volume", "trade_count", "buy_volume", "sell_volume"):
                    if row.get(key) is not None:
                        target[key] = row[key]
                if record_type == "ohlcv" and row.get("close") is not None and target.get("trade_price") is None:
                    target["trade_price"] = row["close"]
            elif record_type == "orderbook":
                for key in ("best_bid", "best_ask", "spread"):
                    if row.get(key) is not None:
                        target[key] = row[key]
            elif record_type == "feature":
                for key in (
                    "spread",
                    "imbalance",
                    "momentum",
                    "volatility",
                    "depth",
                    "whale_score",
                    "time_remaining",
                ):
                    if row.get(key) is not None:
                        target[key] = row[key]
            elif record_type == "trade":
                if row.get("price") is not None:
                    target["trade_price"] = row["price"]
                if row.get("size") is not None:
                    target["trade_size"] = float(target.get("trade_size") or 0) + float(row["size"])
                if row.get("side") is not None:
                    target["trade_side"] = row["side"]
            else:
                for key in COLUMNS:
                    if key not in {"timestamp", "market_id"} and row.get(key) is not None:
                        target[key] = row[key]

        return list(seconds.values())

    def buffer_market_rows(self, market_id: str, slug: str, rows: list[dict[str, Any]]) -> int:
        """
        Accumulate history rows in memory (no disk I/O).
        Call flush_history_buffer() once after the download batch.
        """
        flat = self._flatten_typed_rows(market_id, rows)
        if not flat:
            return 0
        with self._lock:
            self._meta[market_id] = {"market_id": market_id, "slug": slug}
            for row in flat:
                day_key = _floor_second(row["timestamp"]).strftime("%Y-%m-%d")
                self._history_buffer[day_key].append(row)
            self._history_pending.add(market_id)
        return len(flat)

    def flush_history_buffer(self) -> list[Path]:
        """Write each buffered day once, then mark markets done."""
        with self._lock:
            by_day = {k: list(v) for k, v in self._history_buffer.items() if v}
            pending = set(self._history_pending)

        if not by_day:
            return []

        paths: list[Path] = []
        for day_key in sorted(by_day):
            day_rows = by_day[day_key]
            df = self._to_dataframe(day_rows)
            path = self._merge_write_day(day_key, df)
            paths.append(path)
            logger.info(
                "History day={} rows={} markets≈{} path={}",
                day_key,
                len(day_rows),
                len({r.get("market_id") for r in day_rows}),
                path.name,
            )

        with self._lock:
            for day_key in by_day:
                self._history_buffer.pop(day_key, None)
            self._history_pending -= pending
            self._done_markets.update(pending)
            self._save_done()
        return paths

    def write_market_rows(self, market_id: str, slug: str, rows: list[dict[str, Any]]) -> list[Path]:
        """Buffer one market then flush immediately (single-market path)."""
        if not self.buffer_market_rows(market_id, slug, rows):
            return []
        return self.flush_history_buffer()

    # backwards-compatible alias
    def write_market_file(self, market_id: str, slug: str, rows: list[dict[str, Any]]) -> Path | None:
        paths = self.write_market_rows(market_id, slug, rows)
        return paths[0] if paths else None

    @staticmethod
    def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        if "timestamp" in out.columns:
            out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
        if "market_id" in out.columns:
            out["market_id"] = out["market_id"].astype(str)
        for col in list(out.columns):
            if col in {"timestamp", "market_id", "trade_side"}:
                continue
            if out[col].dtype == object:
                sample = out[col].dropna().head(1)
                if len(sample) and isinstance(sample.iloc[0], (dict, list)):
                    out = out.drop(columns=[col])
        return out


sessions = MarketSessionStore()
