from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import settings
from app.features.depth_bands import ORDERBOOK_COLUMNS
from app.features.trade_schema import TRADE_COLUMNS
from app.utils.logger import logger
from app.utils.time import utcnow


TABLE_COLUMNS: dict[str, list[str]] = {
    "btc": ["timestamp", "price"],
    "orderbooks": list(ORDERBOOK_COLUMNS),
    "trades": list(TRADE_COLUMNS),
    "orders": ["timestamp", "order_id", "wallet", "price", "quantity", "event_type"],
}

# map legacy / collector record_type -> table name
RECORD_TO_TABLE = {
    "btc": "btc",
    "btc_tick": "btc",
    "btc_1s": "btc",
    "btc_ticks": "btc",
    "orderbook": "orderbooks",
    "trade": "trades",
    "order": "orders",
}


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


def _json_cell(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


class MarketSessionStore:
    """
    Analysis-grade multi-table store, one directory per market:

      {data_dir}/by_market/{slug}/
        meta.json
        btc.parquet
        orderbooks.parquet
        trades.parquet
        orders.parquet
    """

    def __init__(self, data_dir: Path | None = None, compression: str | None = None) -> None:
        self.data_dir = Path(data_dir or settings.data_dir)
        self.compression = compression or settings.parquet_compression
        self._meta: dict[str, dict[str, Any]] = {}
        self._done_markets: set[str] = set()
        # market_id -> table -> rows
        self._buffers: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: {name: [] for name in TABLE_COLUMNS}
        )
        self._lock = threading.Lock()
        self._market_locks: dict[str, threading.Lock] = {}
        self._load_done()

    @property
    def root(self) -> Path:
        path = self.data_dir / "by_market"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def market_dir(self, market_id: str, slug: str | None = None) -> Path:
        with self._lock:
            meta = self._meta.get(market_id) or {}
        name = slug or meta.get("slug") or market_id
        path = self.root / safe_name(str(name))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _market_lock(self, market_id: str) -> threading.Lock:
        with self._lock:
            if market_id not in self._market_locks:
                self._market_locks[market_id] = threading.Lock()
            return self._market_locks[market_id]

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
            prev = self._meta.get(market_id) or {}
            self._meta[market_id] = {**prev, **market, "market_id": market_id}

    def write_meta(self, market_id: str, meta: dict[str, Any] | None = None, slug: str | None = None) -> Path:
        from app.features.meta_schema import build_meta_document

        with self._lock:
            payload = dict(meta or self._meta.get(market_id) or {})
            if slug:
                payload["slug"] = slug
            self._meta[market_id] = {**self._meta.get(market_id, {}), **payload, "market_id": market_id}
            slug_final = payload.get("slug") or self._meta[market_id].get("slug") or market_id
        path = self.market_dir(market_id, slug=str(slug_final)) / "meta.json"
        doc = build_meta_document({**payload, "market_id": market_id})
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return path

    def append(self, market_id: str, record_type: str, row: dict[str, Any]) -> None:
        if not market_id or market_id in self._done_markets:
            return
        table = RECORD_TO_TABLE.get(record_type, record_type)
        if table not in TABLE_COLUMNS:
            return
        cleaned = self._normalize_row(table, row)
        if table == "trades" and cleaned.get("timestamp") is None:
            return
        with self._lock:
            self._buffers[market_id][table].append(cleaned)

    def append_btc_tick_to_active(self, tick: dict[str, Any], active_markets: list[Any]) -> None:
        ts = tick.get("timestamp")
        price = tick.get("price")
        if ts is None or price is None:
            return
        from app.features.depth_bands import timestamp_to_ms

        row = {"timestamp": (timestamp_to_ms(ts) // 1000) * 1000, "price": float(price)}
        for market in active_markets:
            start = getattr(market, "start_time", None)
            end = getattr(market, "end_time", None) or getattr(market, "settlement_time", None)
            if start and ts < start:
                continue
            if end and ts > end:
                continue
            self.append(market.market_id, "btc", row)

    def append_btc_to_active(self, bar: dict[str, Any], active_markets: list[Any]) -> None:
        """Back-compat: treat 1s bar close as last traded price."""
        self.append_btc_tick_to_active(
            {"timestamp": bar.get("timestamp"), "price": bar.get("close") or bar.get("price")},
            active_markets,
        )

    def _normalize_row(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        cols = TABLE_COLUMNS[table]
        if table == "trades":
            from app.features.trade_schema import build_trade_row

            built = build_trade_row(
                timestamp=row.get("timestamp"),
                wallet=row.get("wallet"),
                price=float(row.get("price") or 0),
                shares=row.get("shares"),
                size=row.get("size"),
                side=row.get("side"),
                outcome=row.get("outcome"),
                asset=row.get("asset"),
                token_yes=row.get("token_yes"),
                token_no=row.get("token_no"),
                token=row.get("token"),
            )
            if built is None:
                return {c: None for c in cols}
            return built
        if table in {"orderbooks", "btc"}:
            out = {c: row.get(c) for c in cols}
            if out.get("timestamp") is not None and not isinstance(out["timestamp"], (int, float)):
                from app.features.depth_bands import timestamp_to_ms

                out["timestamp"] = timestamp_to_ms(out["timestamp"])
            if table == "btc" and out.get("timestamp") is not None:
                out["timestamp"] = (int(out["timestamp"]) // 1000) * 1000
            return out
        out: dict[str, Any] = {}
        for col in cols:
            if col == "timestamp":
                out[col] = row.get("timestamp") or utcnow()
            elif col == "quantity":
                out[col] = row.get("quantity", row.get("size"))
            else:
                out[col] = row.get(col)
        return out

    def _to_dataframe(self, table: str, rows: list[dict[str, Any]]) -> pd.DataFrame:
        cols = TABLE_COLUMNS[table]
        if not rows:
            return pd.DataFrame(columns=cols)
        cleaned = [self._normalize_row(table, r) for r in rows]
        cleaned = [r for r in cleaned if r.get("timestamp") is not None] if table == "trades" else cleaned
        df = pd.DataFrame(cleaned)
        for col in cols:
            if col not in df.columns:
                df[col] = None
        df = df[cols]
        if table == "orderbooks":
            df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").astype("int64")
            price_cols = [
                c
                for c in cols
                if c.endswith("_price") or c in {"up_price", "down_price"}
            ]
            share_cols = [c for c in cols if c.endswith("_shares") or any(
                c.endswith(f"_{s}") for s in ("0_1", "1_3", "3_7", "7_15", "15_30", "30_plus")
            )]
            for c in price_cols:
                df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
            for c in share_cols:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).clip(lower=0).astype("uint32")
            return df
        if table == "btc":
            df["timestamp"] = (
                (pd.to_numeric(df["timestamp"], errors="coerce").astype("int64") // 1000) * 1000
            )
            df["price"] = pd.to_numeric(df["price"], errors="coerce").astype("float32")
            return df
        if table == "trades":
            df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").astype("int64")
            df["wallet"] = df["wallet"].fillna("").astype("string")
            df["token"] = df["token"].astype("boolean")
            df["side"] = df["side"].astype("boolean")
            df["price"] = pd.to_numeric(df["price"], errors="coerce").astype("float32")
            df["shares"] = (
                pd.to_numeric(df["shares"], errors="coerce").fillna(0).clip(lower=0).astype("uint32")
            )
            return df
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df

    def _write_table(self, market_id: str, table: str, rows: list[dict[str, Any]], slug: str | None = None) -> Path | None:
        if not rows:
            return None
        df = self._to_dataframe(table, rows)
        path = self.market_dir(market_id, slug=slug) / f"{table}.parquet"
        lock = self._market_lock(market_id)
        with lock:
            if path.exists():
                old = pd.read_parquet(path)
                if "market_id" in old.columns:
                    old = old.drop(columns=["market_id"])
                if table == "btc":
                    old = old[[c for c in ["timestamp", "price"] if c in old.columns]]
                elif table == "trades":
                    from app.features.trade_schema import TRADE_COLUMNS as _TC

                    keep = [c for c in _TC if c in old.columns]
                    if keep == list(_TC):
                        old = old[keep]
                    else:
                        old = df.iloc[0:0]
                combined = pd.concat([old, df], ignore_index=True)
            else:
                combined = df
            if "timestamp" in combined.columns:
                if table == "trades":
                    subset = list(TRADE_COLUMNS)
                elif table == "orders" and "order_id" in combined.columns:
                    subset = ["order_id", "event_type"]
                elif table in {"btc", "orderbooks"}:
                    subset = ["timestamp"]
                else:
                    subset = list(combined.columns)
                combined = combined.drop_duplicates(subset=subset, keep="last")
                combined = combined.sort_values(["timestamp"]).reset_index(drop=True)
            combined.to_parquet(path, index=False, compression=self.compression)
        return path

    def flush(self, market_id: str) -> list[Path]:
        with self._lock:
            buf = {t: list(rows) for t, rows in self._buffers.get(market_id, {}).items()}
            slug = (self._meta.get(market_id) or {}).get("slug")
            for t in TABLE_COLUMNS:
                self._buffers[market_id][t] = []
        if not any(buf.values()):
            return []
        self.write_meta(market_id, slug=slug)
        paths: list[Path] = []
        for table, rows in buf.items():
            path = self._write_table(market_id, table, rows, slug=slug)
            if path:
                paths.append(path)
                logger.info("Flushed {} +{} rows market={} path={}", table, len(rows), market_id, path.name)
        return paths

    def finalize(self, market_id: str) -> list[Path]:
        if not market_id:
            return []
        paths = self.flush(market_id)
        with self._lock:
            self._done_markets.add(market_id)
            self._buffers.pop(market_id, None)
            self._save_done()
        return paths

    def flush_all_active(self) -> int:
        with self._lock:
            ids = [mid for mid in self._buffers if mid not in self._done_markets]
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

    def write_market_bundle(
        self,
        market_id: str,
        slug: str,
        *,
        meta: dict[str, Any],
        tables: dict[str, list[dict[str, Any]]],
    ) -> list[Path]:
        """History path: write all tables for one market at once."""
        self.set_market_meta({**meta, "market_id": market_id, "slug": slug})
        self.write_meta(market_id, meta={**meta, "slug": slug}, slug=slug)
        paths: list[Path] = [self.market_dir(market_id, slug=slug) / "meta.json"]
        for table, rows in tables.items():
            if table not in TABLE_COLUMNS:
                continue
            path = self._write_table(market_id, table, rows, slug=slug)
            if path:
                paths.append(path)
        with self._lock:
            self._done_markets.add(market_id)
            self._save_done()
        logger.info(
            "History bundle slug={} tables={}",
            slug,
            {k: len(v) for k, v in tables.items() if v},
        )
        return paths

    # ---- deprecated flat-row helpers (kept so old callers don't crash) ----
    def buffer_market_rows(self, market_id: str, slug: str, rows: list[dict[str, Any]]) -> int:
        """Legacy: map typed flat rows into multi-table buffers."""
        n = 0
        self.set_market_meta({"market_id": market_id, "slug": slug})
        for row in rows:
            rt = str(row.get("record_type") or "")
            if rt in RECORD_TO_TABLE:
                self.append(market_id, rt, row)
                n += 1
        return n

    def flush_history_buffer(self) -> list[Path]:
        with self._lock:
            ids = list(self._buffers.keys())
        paths: list[Path] = []
        for market_id in ids:
            paths.extend(self.flush(market_id))
            with self._lock:
                self._done_markets.add(market_id)
            self._save_done()
        return paths

    def write_market_rows(self, market_id: str, slug: str, rows: list[dict[str, Any]]) -> list[Path]:
        self.buffer_market_rows(market_id, slug, rows)
        return self.flush_history_buffer()


sessions = MarketSessionStore()
