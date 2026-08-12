"""Fill missed Polymarket trades on disk via Data API (VPS repair)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from loguru import logger

from app.config import settings
from app.polymarket.data_api_trades import (
    DataApiTrades,
    _fill_base_key,
    assign_fill_keys,
    drop_uniform_tx_duplicates,
    normalize_trade_legs,
)
from app.schemas import (
    BINANCE_BAND_COLUMNS,
    ORDERBOOK_COLUMNS,
    SCHEMAS,
    TABLE_FILES,
)
from app.trades_mode import get_trades_mode

_BINANCE_REST = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
)

_TRADE_COLS = (
    "timestamp",
    "transaction_hash",
    "wallet",
    "is_up",
    "is_buy",
    "is_taker",
    "price",
    "shares",
    "fill_index",
)

_locks: dict[str, asyncio.Lock] = {}
_locks_mu = threading.Lock()


def _market_lock(market_id: str) -> asyncio.Lock:
    with _locks_mu:
        lock = _locks.get(market_id)
        if lock is None:
            lock = asyncio.Lock()
            _locks[market_id] = lock
        return lock


def _read_meta(market_dir: Path) -> dict[str, Any] | None:
    path = market_dir / "meta.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_meta(market_dir: Path, meta: dict[str, Any]) -> None:
    path = market_dir / "meta.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _coerce_row(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        ts = int(row.get("timestamp") or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts < 10_000_000_000:
        ts *= 1000
    try:
        price = round(float(row.get("price") or 0), 6)
        shares = round(max(0.0, float(row.get("shares") or 0)), 2)
    except (TypeError, ValueError):
        return None
    try:
        fill_index = int(row.get("fill_index") or 0)
    except (TypeError, ValueError):
        fill_index = 0
    return {
        "timestamp": ts,
        "transaction_hash": str(row.get("transaction_hash") or "").strip().lower(),
        "wallet": str(row.get("wallet") or "").strip().lower(),
        "is_up": bool(row.get("is_up")),
        "is_buy": bool(row.get("is_buy")),
        "is_taker": bool(row.get("is_taker", True)),
        "price": price,
        "shares": shares,
        "fill_index": fill_index,
    }


def load_trade_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        table = pq.read_table(path)
    except Exception as exc:
        logger.warning("Could not read {}: {}", path, exc)
        return []
    out: list[dict[str, Any]] = []
    for raw in table.to_pylist():
        if not isinstance(raw, dict):
            continue
        row = _coerce_row(raw)
        if row is not None:
            out.append(row)
    return out


def write_trade_rows(path: Path, rows: list[dict[str, Any]]) -> int:
    prepared = assign_fill_keys(
        [row for r in rows if (row := _coerce_row(r)) is not None]
    )
    prepared.sort(
        key=lambda r: (
            int(r.get("timestamp") or 0),
            str(r.get("transaction_hash") or ""),
            str(r.get("wallet") or ""),
            int(r.get("fill_index") or 0),
        )
    )
    payload = [{k: r.get(k) for k in _TRADE_COLS} for r in prepared]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    table = pa.Table.from_pylist(payload, schema=SCHEMAS["trades"])
    pq.write_table(table, tmp, compression=settings.parquet_compression)
    tmp.replace(path)
    return len(payload)


def _floor_s(ts: int) -> int:
    return int(ts) - (int(ts) % 1000)


def load_table_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        return [r for r in pq.read_table(path).to_pylist() if isinstance(r, dict)]
    except Exception as exc:
        logger.warning("Could not read {}: {}", path, exc)
        return []


def write_table_rows(path: Path, table: str, rows: list[dict[str, Any]]) -> int:
    schema = SCHEMAS[table]
    cols = [f.name for f in schema]
    payload: list[dict[str, Any]] = []
    for raw in rows:
        row = {c: raw.get(c) for c in cols}
        try:
            row["timestamp"] = int(row.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if row["timestamp"] <= 0:
            continue
        payload.append(row)
    payload.sort(key=lambda r: int(r["timestamp"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(
        pa.Table.from_pylist(payload, schema=schema),
        tmp,
        compression=settings.parquet_compression,
    )
    tmp.replace(path)
    return len(payload)


def _empty_binance_px_row(ts: int, price: float) -> dict[str, Any]:
    row: dict[str, Any] = {"timestamp": int(ts), "Binance_BTC": float(price)}
    for col in BINANCE_BAND_COLUMNS:
        row[col] = 0.0
    return row


async def _binance_json(
    http: Any, path: str, params: dict[str, Any]
) -> Any:
    last_exc: Exception | None = None
    for base in (settings.binance_rest_url, *_BINANCE_REST):
        try:
            resp = await http.get(f"{str(base).rstrip('/')}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            continue
    raise last_exc or RuntimeError("Binance REST failed")


async def fetch_binance_agg_trades(
    http: Any, *, start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor = int(start_ms)
    end = int(end_ms)
    symbol = settings.btc_symbol
    for _ in range(200):
        batch = await _binance_json(
            http,
            "/api/v3/aggTrades",
            {
                "symbol": symbol,
                "startTime": cursor,
                "endTime": end,
                "limit": 1000,
            },
        )
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            if not isinstance(item, dict):
                continue
            try:
                ts = int(item.get("T") or 0)
                price = float(item.get("p") or 0)
                qty = float(item.get("q") or 0)
            except (TypeError, ValueError):
                continue
            if ts < start_ms or ts >= end_ms:
                continue
            out.append(
                {
                    "timestamp": ts,
                    "price": price,
                    "quantity": qty,
                    "buyer_is_maker": bool(item.get("m")),
                }
            )
        last_t = int(batch[-1].get("T") or 0)
        if last_t >= end - 1 or len(batch) < 1000:
            break
        cursor = last_t + 1
        if cursor >= end:
            break
    return out


async def fetch_binance_klines_1s(
    http: Any, *, start_ms: int, end_ms: int
) -> dict[int, float]:
    out: dict[int, float] = {}
    cursor = int(start_ms)
    end = int(end_ms)
    symbol = settings.btc_symbol
    for _ in range(20):
        batch = await _binance_json(
            http,
            "/api/v3/klines",
            {
                "symbol": symbol,
                "interval": "1s",
                "startTime": cursor,
                "endTime": end - 1,
                "limit": 1000,
            },
        )
        if not isinstance(batch, list) or not batch:
            break
        last_open = cursor
        for item in batch:
            if not isinstance(item, (list, tuple)) or len(item) < 5:
                continue
            try:
                open_ms = _floor_s(int(item[0]))
                close_px = float(item[4])
            except (TypeError, ValueError):
                continue
            if start_ms <= open_ms < end_ms:
                out[open_ms] = close_px
            last_open = open_ms
        if last_open >= end - 1000 or len(batch) < 1000:
            break
        cursor = last_open + 1000
        if cursor >= end:
            break
    return out


def merge_binance_trades(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    seen: set[tuple[int, float, float, bool]] = set()
    out: list[dict[str, Any]] = []
    for src in (existing, incoming):
        for raw in src:
            try:
                ts = int(raw.get("timestamp") or 0)
                price = float(raw.get("price") or 0)
                qty = float(raw.get("quantity") or 0)
                maker = bool(raw.get("buyer_is_maker"))
            except (TypeError, ValueError):
                continue
            key = (ts, round(price, 6), round(qty, 8), maker)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "timestamp": ts,
                    "price": price,
                    "quantity": qty,
                    "buyer_is_maker": maker,
                }
            )
    out.sort(key=lambda r: int(r["timestamp"]))
    return out


def fill_binance_px_1s(
    existing: list[dict[str, Any]],
    klines: dict[int, float],
    *,
    start_ms: int,
    end_ms: int,
) -> tuple[list[dict[str, Any]], int]:
    by_ts: dict[int, dict[str, Any]] = {}
    for raw in existing:
        try:
            ts = _floor_s(int(raw.get("timestamp") or 0))
        except (TypeError, ValueError):
            continue
        if start_ms <= ts < end_ms:
            by_ts[ts] = dict(raw)
            by_ts[ts]["timestamp"] = ts
    added = 0
    last_px: float | None = None
    for ts in range(_floor_s(start_ms), _floor_s(end_ms), 1000):
        if ts in by_ts and by_ts[ts].get("Binance_BTC") is not None:
            try:
                last_px = float(by_ts[ts]["Binance_BTC"])
            except (TypeError, ValueError):
                pass
            continue
        px = klines.get(ts, last_px)
        if px is None:
            continue
        last_px = float(px)
        if ts not in by_ts:
            by_ts[ts] = _empty_binance_px_row(ts, last_px)
            added += 1
        else:
            by_ts[ts]["Binance_BTC"] = last_px
            for col in BINANCE_BAND_COLUMNS:
                by_ts[ts].setdefault(col, 0.0)
            added += 1
    rows = [by_ts[k] for k in sorted(by_ts)]
    return rows, added


def fill_chainlink_1s(
    existing: list[dict[str, Any]],
    klines: dict[int, float],
    *,
    start_ms: int,
    end_ms: int,
) -> tuple[list[dict[str, Any]], int]:
    by_ts: dict[int, dict[str, Any]] = {}
    for raw in existing:
        try:
            ts = _floor_s(int(raw.get("timestamp") or 0))
        except (TypeError, ValueError):
            continue
        if start_ms <= ts < end_ms:
            by_ts[ts] = {
                "timestamp": ts,
                "Chainlink_BTC": raw.get("Chainlink_BTC"),
                "twap": raw.get("twap"),
            }
    added = 0
    last_px: float | None = None
    for ts in range(_floor_s(start_ms), _floor_s(end_ms), 1000):
        row = by_ts.get(ts)
        spot = None
        if row is not None and row.get("Chainlink_BTC") is not None:
            try:
                spot = float(row["Chainlink_BTC"])
            except (TypeError, ValueError):
                spot = None
        if spot is None:
            spot = klines.get(ts, last_px)
            if spot is None:
                continue
            if row is None:
                by_ts[ts] = {"timestamp": ts, "Chainlink_BTC": float(spot), "twap": None}
                added += 1
            else:
                row["Chainlink_BTC"] = float(spot)
                added += 1
        last_px = float(spot)
    # Recompute 30s TWAP only where missing.
    times = sorted(by_ts)
    spots: list[tuple[int, float]] = []
    for ts in times:
        try:
            spots.append((ts, float(by_ts[ts]["Chainlink_BTC"])))
        except (TypeError, ValueError, KeyError):
            continue
    for ts, _px in spots:
        if by_ts[ts].get("twap") is not None:
            continue
        window = [p for t, p in spots if ts - 30_000 < t <= ts]
        if window:
            by_ts[ts]["twap"] = float(sum(window) / len(window))
    return [by_ts[k] for k in sorted(by_ts)], added


def fill_orderbooks_1s(
    existing: list[dict[str, Any]], *, start_ms: int, end_ms: int
) -> tuple[list[dict[str, Any]], int]:
    by_ts: dict[int, dict[str, Any]] = {}
    for raw in existing:
        try:
            ts = _floor_s(int(raw.get("timestamp") or 0))
        except (TypeError, ValueError):
            continue
        if start_ms <= ts < end_ms:
            row = {c: raw.get(c) for c in ORDERBOOK_COLUMNS}
            row["timestamp"] = ts
            by_ts[ts] = row
    if not by_ts:
        return existing, 0
    keys = sorted(by_ts)
    added = 0
    last: dict[str, Any] | None = None
    # Forward fill, then backfill leading hole from first snapshot.
    first = by_ts[keys[0]]
    for ts in range(_floor_s(start_ms), _floor_s(end_ms), 1000):
        if ts in by_ts:
            last = by_ts[ts]
            continue
        src = last or first
        row = dict(src)
        row["timestamp"] = ts
        by_ts[ts] = row
        added += 1
    return [by_ts[k] for k in sorted(by_ts)], added


def _merge_tx(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the richer multiset of Orbscan legs for one transaction."""
    if not new:
        return list(old)
    if not old:
        return list(new)
    oc = Counter(_fill_base_key(r) for r in old)
    nc = Counter(_fill_base_key(r) for r in new)
    if oc == nc:
        return list(new)
    if set(oc) == set(nc) and nc:
        ratios = {oc[k] // nc[k] for k in nc if nc[k] > 0 and oc[k] % nc[k] == 0}
        if len(ratios) == 1 and next(iter(ratios)) >= 2:
            return list(new)
    if (
        set(oc) == set(nc)
        and any(v >= 2 for v in nc.values())
        and len(new) < len(old)
    ):
        return list(new)
    if len(new) >= len(old) and all(nc[k] >= oc.get(k, 0) for k in nc):
        return list(new)

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


def merge_trade_rows(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Add Data API fills without dropping local rows the API omitted."""
    old = normalize_trade_legs(existing)
    new = normalize_trade_legs(drop_uniform_tx_duplicates(incoming))
    if not new:
        return old
    if not old:
        return new

    old_by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    new_by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    old_orphans: list[dict[str, Any]] = []
    new_orphans: list[dict[str, Any]] = []
    for r in old:
        tx = str(r.get("transaction_hash") or "").strip().lower()
        if tx:
            old_by_tx[tx].append(r)
        else:
            old_orphans.append(r)
    for r in new:
        tx = str(r.get("transaction_hash") or "").strip().lower()
        if tx:
            new_by_tx[tx].append(r)
        else:
            new_orphans.append(r)

    out: list[dict[str, Any]] = []
    for tx in set(old_by_tx) | set(new_by_tx):
        out.extend(_merge_tx(old_by_tx.get(tx, []), new_by_tx.get(tx, [])))
    out.extend(old_orphans)
    seen_orphan = {_fill_base_key(r) for r in old_orphans}
    for r in new_orphans:
        key = _fill_base_key(r)
        if key in seen_orphan:
            continue
        seen_orphan.add(key)
        out.append(r)
    return assign_fill_keys(out)


def _market_is_live(meta: dict[str, Any]) -> bool:
    if not bool(meta.get("active")):
        return False
    try:
        end_ms = int(meta.get("end_time") or 0)
    except (TypeError, ValueError):
        return False
    return end_ms <= 0 or int(time.time() * 1000) < end_ms


async def repair_market_dir(market_dir: Path) -> dict[str, Any]:
    """
    Fill missed history for a finished market:
      - Polymarket trades (Data API, respects trades_mode)
      - Binance aggTrades
      - 1s Binance_BTC (klines; depth bands stay 0 where reconstructed)
      - 1s Chainlink/TWAP holes from Binance 1s close
      - 1s orderbooks via nearest-snapshot fill
    """
    meta = _read_meta(market_dir)
    if not meta:
        return {"ok": False, "error": "meta.json missing or invalid"}
    market_id = str(meta.get("market_id") or market_dir.name)
    if _market_is_live(meta):
        return {
            "ok": False,
            "market_id": market_id,
            "error": "market is still live — repair after it rolls off",
        }
    try:
        start_ms = int(meta.get("start_time") or 0)
        end_ms = int(meta.get("end_time") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "market_id": market_id, "error": "invalid start/end time"}
    if start_ms <= 0 or end_ms <= start_ms:
        return {"ok": False, "market_id": market_id, "error": "invalid market window"}

    mode = get_trades_mode()
    filled: dict[str, int] = {}
    errors: list[str] = []

    cid = str(meta.get("condition_id") or "").strip()
    pm_before = 0
    pm_after = 0
    pm_api = 0
    if cid:
        token_up = str(meta.get("up_token_id") or "").strip() or None
        token_down = str(meta.get("down_token_id") or "").strip() or None
        trades_path = market_dir / TABLE_FILES["trades"]
        before = load_trade_rows(trades_path)
        pm_before = len(before)
        client = DataApiTrades()
        try:
            incoming = await client.fetch_window(
                condition_id=cid,
                token_up=token_up,
                token_down=token_down,
                start_ms=start_ms,
                end_ms=end_ms,
                max_pages=50,
                trades_mode=mode,
            )
            pm_api = len(incoming)
            merged = merge_trade_rows(before, incoming)
            if mode == "taker":
                merged = [r for r in merged if bool(r.get("is_taker", True))]
                merged = assign_fill_keys(merged)
            pm_after = write_trade_rows(trades_path, merged)
            filled["trades.parquet"] = max(0, pm_after - pm_before)
            meta["trades_repaired_complete"] = True
        except Exception as exc:
            logger.warning("VPS PM trade repair failed for {}: {}", market_id, exc)
            errors.append(f"trades.parquet: {exc}")
        finally:
            await client.close()
    else:
        errors.append("trades.parquet: no condition_id")

    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=8.0)) as http:
        klines: dict[int, float] = {}
        try:
            klines = await fetch_binance_klines_1s(http, start_ms=start_ms, end_ms=end_ms)
        except Exception as exc:
            errors.append(f"binance 1s klines: {exc}")
        try:
            incoming_bt = await fetch_binance_agg_trades(
                http, start_ms=start_ms, end_ms=end_ms
            )
            bt_path = market_dir / TABLE_FILES["binance_trades"]
            old_bt = load_table_rows(bt_path)
            merged_bt = merge_binance_trades(old_bt, incoming_bt)
            write_table_rows(bt_path, "binance_trades", merged_bt)
            filled["binance_trades.parquet"] = max(0, len(merged_bt) - len(old_bt))
        except Exception as exc:
            errors.append(f"binance_trades.parquet: {exc}")

        if klines:
            try:
                px_path = market_dir / TABLE_FILES["binance_price_orderbook"]
                px_rows, n = fill_binance_px_1s(
                    load_table_rows(px_path), klines, start_ms=start_ms, end_ms=end_ms
                )
                write_table_rows(px_path, "binance_price_orderbook", px_rows)
                filled["binance_price_orderbook.parquet"] = n
            except Exception as exc:
                errors.append(f"binance_price_orderbook.parquet: {exc}")
            try:
                cl_path = market_dir / TABLE_FILES["chainlink_price"]
                cl_rows, n = fill_chainlink_1s(
                    load_table_rows(cl_path), klines, start_ms=start_ms, end_ms=end_ms
                )
                write_table_rows(cl_path, "chainlink_price", cl_rows)
                filled["chainlink_price.parquet"] = n
            except Exception as exc:
                errors.append(f"chainlink_price.parquet: {exc}")

    try:
        ob_path = market_dir / TABLE_FILES["orderbooks"]
        ob_rows, n = fill_orderbooks_1s(
            load_table_rows(ob_path), start_ms=start_ms, end_ms=end_ms
        )
        if n:
            write_table_rows(ob_path, "orderbooks", ob_rows)
        filled["orderbooks.parquet"] = n
    except Exception as exc:
        errors.append(f"orderbooks.parquet: {exc}")

    now_ms = int(time.time() * 1000)
    meta["trades_mode"] = mode
    meta["trades_repaired_at"] = now_ms
    if pm_after:
        meta["trades_count"] = pm_after
    meta["repair_filled"] = filled
    _write_meta(market_dir, meta)

    added = int(filled.get("trades.parquet") or 0)
    logger.info("Repaired market {} filled={} errors={}", market_id, filled, errors)
    return {
        "ok": True,
        "market_id": market_id,
        "trades_mode": mode,
        "rows_before": pm_before,
        "rows_from_api": pm_api,
        "rows_after": pm_after,
        "rows_added": added,
        "filled": filled,
        "errors": errors,
        "repaired_at": now_ms,
    }


async def repair_market_dir_locked(market_dir: Path) -> dict[str, Any]:
    market_id = str(market_dir.name)
    async with _market_lock(market_id):
        return await repair_market_dir(market_dir)
