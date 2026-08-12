"""Fill missed Polymarket trades on disk via Data API (VPS repair)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

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
from app.schemas import SCHEMAS, TABLE_FILES
from app.trades_mode import get_trades_mode

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
    Fetch Data API trades for this market and merge into trades.parquet.

    Respects current trades_mode (taker vs full). Refuses while the market
    is still the live collector window (in-memory flush would overwrite).
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

    cid = str(meta.get("condition_id") or "").strip()
    if not cid:
        return {
            "ok": False,
            "market_id": market_id,
            "error": "no condition_id in meta.json",
        }
    try:
        start_ms = int(meta.get("start_time") or 0)
        end_ms = int(meta.get("end_time") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "market_id": market_id, "error": "invalid start/end time"}
    if start_ms <= 0 or end_ms <= start_ms:
        return {"ok": False, "market_id": market_id, "error": "invalid market window"}

    mode = get_trades_mode()
    token_up = str(meta.get("up_token_id") or "").strip() or None
    token_down = str(meta.get("down_token_id") or "").strip() or None
    trades_path = market_dir / TABLE_FILES["trades"]
    before = load_trade_rows(trades_path)

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
    except Exception as exc:
        logger.warning("VPS trade repair fetch failed for {}: {}", market_id, exc)
        return {
            "ok": False,
            "market_id": market_id,
            "error": f"Data API fetch failed: {exc}",
            "trades_mode": mode,
        }
    finally:
        await client.close()

    merged = merge_trade_rows(before, incoming)
    if mode == "taker":
        merged = [r for r in merged if bool(r.get("is_taker", True))]
        merged = assign_fill_keys(merged)
    written = write_trade_rows(trades_path, merged)
    added = max(0, written - len(before))

    now_ms = int(time.time() * 1000)
    meta["trades_mode"] = mode
    meta["trades_repaired_at"] = now_ms
    meta["trades_count"] = written
    _write_meta(market_dir, meta)

    logger.info(
        "Repaired market {} mode={} before={} api={} after={} (+{})",
        market_id,
        mode,
        len(before),
        len(incoming),
        written,
        added,
    )
    return {
        "ok": True,
        "market_id": market_id,
        "trades_mode": mode,
        "rows_before": len(before),
        "rows_from_api": len(incoming),
        "rows_after": written,
        "rows_added": added,
        "repaired_at": now_ms,
    }


async def repair_market_dir_locked(market_dir: Path) -> dict[str, Any]:
    market_id = str(market_dir.name)
    async with _market_lock(market_id):
        return await repair_market_dir(market_dir)
