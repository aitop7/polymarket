"""Repair sparse local trades.parquet from Polymarket Data API."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

DATA_API_URL = "https://data-api.polymarket.com"
_TRADE_COLS = [
    "timestamp",
    "transaction_hash",
    "wallet",
    "is_up",
    "is_buy",
    "is_taker",
    "price",
    "shares",
    "fill_index",
]


def _meta(market_dir: Path) -> dict[str, Any] | None:
    path = market_dir / "meta.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _taker_key(tx: str, wallet: str) -> str:
    return f"{tx.strip().lower()}|{wallet.strip().lower()}"


def shares_2(value: Any) -> float:
    """Non-negative share amount with 2 decimal places."""
    try:
        return round(max(0.0, float(value or 0)), 2)
    except (TypeError, ValueError):
        return 0.0


def taker_wallets_by_tx(taker_raw: list[dict[str, Any]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for trade in taker_raw:
        tx = str(trade.get("transactionHash") or trade.get("transaction_hash") or "").strip()
        wallet = str(
            trade.get("proxyWallet")
            or trade.get("proxy_wallet")
            or trade.get("wallet")
            or ""
        ).strip()
        if not tx or not wallet:
            continue
        out.setdefault(tx.lower(), set()).add(wallet.lower())
    return out


def classify_is_taker(
    tx: str, wallet: str, takers_by_tx: dict[str, set[str]]
) -> bool:
    """Only mark maker when this tx has a known taker; else default taker."""
    tx_l = (tx or "").strip().lower()
    w_l = (wallet or "").strip().lower()
    if not tx_l or not w_l:
        return True
    known = takers_by_tx.get(tx_l)
    if not known:
        return True
    return w_l in known


def _raw_fill_key(trade: dict[str, Any]) -> str:
    tx = str(trade.get("transactionHash") or trade.get("transaction_hash") or "")
    wallet = str(
        trade.get("proxyWallet")
        or trade.get("proxy_wallet")
        or trade.get("wallet")
        or ""
    )
    asset = str(trade.get("asset") or trade.get("asset_id") or "")
    side = str(trade.get("side") or "").upper()
    try:
        size = f"{round(float(trade.get('size') or 0), 2):.2f}"
    except (TypeError, ValueError):
        size = str(trade.get("size") or "")
    try:
        price = f"{round(float(trade.get('price') or 0), 6):.6f}"
    except (TypeError, ValueError):
        price = str(trade.get("price") or "")
    outcome = str(trade.get("outcome") or trade.get("outcomeIndex") or "").strip().lower()
    return f"{tx.lower()}|{wallet.lower()}|{asset}|{side}|{size}|{price}|{outcome}"


def _merge_trade_raw(
    all_raw: list[dict[str, Any]], taker_raw: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Prefer all_raw; only append taker_raw rows missing after rounded-key match."""
    from collections import Counter

    merged: list[dict[str, Any]] = list(all_raw)
    covered = Counter(_raw_fill_key(t) for t in all_raw)
    for trade in taker_raw:
        key = _raw_fill_key(trade)
        if covered[key] > 0:
            covered[key] -= 1
            continue
        merged.append(trade)
    return merged


def _fill_base_key(row: dict[str, Any]) -> str:
    tx = str(row.get("transaction_hash") or "").strip().lower()
    wallet = str(row.get("wallet") or "").strip().lower()
    is_up = int(bool(row.get("is_up")))
    is_buy = int(bool(row.get("is_buy")))
    try:
        price = f"{round(float(row.get('price') or 0), 6):.6f}"
    except (TypeError, ValueError):
        price = "0.000000"
    try:
        shares = f"{shares_2(row.get('shares')):.2f}"
    except (TypeError, ValueError):
        shares = "0.00"
    if tx:
        return f"{tx}|{wallet}|{is_up}|{is_buy}|{price}|{shares}"
    return (
        f"notx:{int(row.get('timestamp') or 0)}|{wallet}|{is_up}|{is_buy}|{price}|{shares}"
    )


def trim_page_overlap(
    prev_keys: list[str], batch: list[dict[str, Any]], batch_keys: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Drop leading rows of a page that repeat the tail of the previous pages.

    The Data API is newest-first and offset-paged, so trades arriving mid-fetch
    push the list down and ``offset=500`` re-returns rows already read. The
    repeat is a contiguous prefix/suffix, so trimming the longest match removes
    the paging artifact without touching genuine identical fills.
    """
    max_k = min(len(prev_keys), len(batch_keys))
    for k in range(max_k, 0, -1):
        if prev_keys[-k:] == batch_keys[:k]:
            return batch[k:], batch_keys[k:]
    return batch, batch_keys


def drop_uniform_tx_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Undo whole-transaction duplication left by a feed that replayed rows.

    Page overlap repeats a contiguous block, so an affected tx returns every one
    of its distinct legs multiplied by the same factor. Dividing by the gcd of
    the leg counts restores the real fills; genuine repeated legs never share a
    factor across the whole tx (the taker leg is unique).
    """
    from collections import Counter, defaultdict
    from math import gcd

    if not rows:
        return []
    by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    out: list[dict[str, Any]] = []
    for row in rows:
        tx = str(row.get("transaction_hash") or "").strip().lower()
        if not tx:
            out.append(row)
            continue
        by_tx[tx].append(row)

    for group in by_tx.values():
        counts = Counter(_fill_base_key(r) for r in group)
        factor = 0
        for n in counts.values():
            factor = gcd(factor, n)
        if len(counts) < 2 or factor < 2:
            out.extend(group)
            continue
        kept: Counter[str] = Counter()
        for r in group:
            key = _fill_base_key(r)
            if kept[key] >= counts[key] // factor:
                continue
            kept[key] += 1
            out.append(r)
    return out


def assign_fill_keys(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep every fill as its own row; stable fill_key with occurrence index."""
    from collections import Counter

    if not rows:
        return []
    prepared: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        r["shares"] = shares_2(r.get("shares"))
        try:
            r["price"] = round(float(r.get("price") or 0), 6)
        except (TypeError, ValueError):
            r["price"] = 0.0
        prepared.append(r)
    prepared.sort(
        key=lambda r: (
            _fill_base_key(r),
            int(r.get("fill_index") or 0),
            str(r.get("fill_key") or ""),
        )
    )
    counts: Counter[str] = Counter()
    out: list[dict[str, Any]] = []
    for r in prepared:
        base = _fill_base_key(r)
        occ = counts[base]
        counts[base] += 1
        r["fill_index"] = int(occ)
        r["fill_key"] = f"{base}|{occ}"
        out.append(r)
    return out


def aggregate_wallet_shares(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Backward-compatible name: do not merge wallets."""
    return assign_fill_keys(rows)


def normalize_trade_legs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Orbscan-style legs:
      - keep every fill row (same wallet may appear several times)
      - keep each row's is_up / is_buy / price
      - one primary taker wallet; other wallets demoted to maker
    """
    if not rows:
        return []
    by_tx: dict[str, list[dict[str, Any]]] = {}
    orphan: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        r["wallet"] = str(r.get("wallet") or "").strip().lower()
        tx = str(r.get("transaction_hash") or "").strip().lower()
        if not tx:
            orphan.append(r)
            continue
        by_tx.setdefault(tx, []).append(r)

    out: list[dict[str, Any]] = list(orphan)
    for group in by_tx.values():
        takers = [r for r in group if r.get("is_taker")]
        if not takers:
            if len({str(r.get("wallet") or "").lower() for r in group}) <= 1:
                out.extend(group)
                continue
            takers = list(group)
        taker = min(
            takers,
            key=lambda r: (
                -float(r.get("shares") or 0),
                0 if r.get("is_buy") else 1,
                int(r.get("timestamp") or 0),
                str(r.get("wallet") or "").lower(),
            ),
        )
        taker_wallet = str(taker.get("wallet") or "").strip().lower()
        for r in group:
            r["is_taker"] = str(r.get("wallet") or "").strip().lower() == taker_wallet
            out.append(r)
    return assign_fill_keys(out)


def _row_from_api(
    trade: dict[str, Any],
    *,
    token_up: str | None,
    token_down: str | None,
    start_ms: int,
    end_ms: int,
    takers_by_tx: dict[str, set[str]],
) -> dict[str, Any] | None:
    try:
        ts = int(trade.get("timestamp") or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts < 10_000_000_000:
        ts *= 1000
    # Include settlement prints shortly after end (volume often lands late).
    if end_ms and ts >= int(end_ms) + 30 * 60_000:
        return None
    if start_ms and ts < int(start_ms) - 300_000:
        return None

    asset = str(trade.get("asset") or trade.get("asset_id") or "")
    is_up: bool | None = None
    if token_up and asset == str(token_up):
        is_up = True
    elif token_down and asset == str(token_down):
        is_up = False
    else:
        outcome = str(trade.get("outcome") or "").strip().lower()
        if outcome in {"up", "yes"}:
            is_up = True
        elif outcome in {"down", "no"}:
            is_up = False
        else:
            try:
                is_up = int(trade.get("outcomeIndex")) == 0
            except (TypeError, ValueError):
                return None
    if is_up is None:
        return None

    side_raw = str(trade.get("side") or "BUY").upper()
    is_buy = side_raw not in {"SELL", "S"}
    try:
        price = float(trade.get("price") or 0)
        size = float(trade.get("size") or 0)
    except (TypeError, ValueError):
        return None
    if size <= 0 or price < 0:
        return None

    wallet = str(
        trade.get("proxyWallet")
        or trade.get("proxy_wallet")
        or trade.get("wallet")
        or ""
    ).strip().lower()
    tx = str(trade.get("transactionHash") or trade.get("transaction_hash") or "")
    is_taker = classify_is_taker(tx, wallet, takers_by_tx)

    shares = shares_2(size)
    return {
        "timestamp": ts,
        "transaction_hash": tx,
        "wallet": wallet,
        "is_up": bool(is_up),
        "is_buy": bool(is_buy),
        "is_taker": bool(is_taker),
        "price": float(price),
        "shares": shares,
    }


async def _fetch_pages(
    http: httpx.AsyncClient,
    *,
    condition_id: str,
    max_pages: int,
    start_ms: int,
    taker_only: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    out_keys: list[str] = []
    offset = 0
    for _ in range(max_pages):
        resp = await http.get(
            "/trades",
            params={
                "market": condition_id,
                "limit": 500,
                "offset": offset,
                "takerOnly": str(taker_only).lower(),
            },
        )
        resp.raise_for_status()
        raw = resp.json()
        batch = raw if isinstance(raw, list) else []
        if not batch:
            break
        page = [item for item in batch if isinstance(item, dict)]
        page, page_keys = trim_page_overlap(
            out_keys, page, [_raw_fill_key(t) for t in page]
        )
        out.extend(page)
        out_keys.extend(page_keys)
        oldest: int | None = None
        for item in batch:
            try:
                raw_ts = int(item.get("timestamp") or 0)
            except (TypeError, ValueError):
                continue
            if raw_ts <= 0:
                continue
            if raw_ts < 10_000_000_000:
                raw_ts *= 1000
            oldest = raw_ts if oldest is None else min(oldest, raw_ts)
        if oldest is not None and start_ms and oldest < int(start_ms) - 300_000:
            break
        if len(batch) < 500:
            break
        offset += 500
    return out


async def fetch_data_api_trades(
    *,
    condition_id: str,
    token_up: str | None,
    token_down: str | None,
    start_ms: int,
    end_ms: int,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    if not condition_id:
        return []
    async with httpx.AsyncClient(
        base_url=DATA_API_URL,
        timeout=httpx.Timeout(25.0, connect=8.0),
    ) as http:
        all_raw, taker_raw = await asyncio.gather(
            _fetch_pages(
                http,
                condition_id=condition_id,
                max_pages=max_pages,
                start_ms=start_ms,
                taker_only=False,
            ),
            _fetch_pages(
                http,
                condition_id=condition_id,
                max_pages=max_pages,
                start_ms=start_ms,
                taker_only=True,
            ),
        )

    takers_by_tx = taker_wallets_by_tx(taker_raw)

    out: list[dict[str, Any]] = []
    # Fills from takerOnly=false only; takerOnly=true is for classification.
    for item in all_raw:
        row = _row_from_api(
            item,
            token_up=token_up,
            token_down=token_down,
            start_ms=start_ms,
            end_ms=end_ms,
            takers_by_tx=takers_by_tx,
        )
        if row is None:
            continue
        out.append(row)
    return normalize_trade_legs(drop_uniform_tx_duplicates(out))


def write_trades_parquet(path: Path, rows: list[dict[str, Any]]) -> int:
    """Replace trades.parquet with freshly classified rows. Returns row count."""
    if not rows:
        return 0
    rows = normalize_trade_legs(rows)
    new_df = pd.DataFrame(rows)
    for col in _TRADE_COLS:
        if col not in new_df.columns:
            if col == "fill_index":
                new_df[col] = 0
            else:
                new_df[col] = "" if col in {"transaction_hash", "wallet"} else False
    new_df = new_df[_TRADE_COLS]
    new_df["timestamp"] = new_df["timestamp"].astype("int64")
    new_df["transaction_hash"] = new_df["transaction_hash"].fillna("").astype(str)
    new_df["wallet"] = new_df["wallet"].astype(str)
    new_df["is_up"] = new_df["is_up"].astype(bool)
    new_df["is_buy"] = new_df["is_buy"].astype(bool)
    new_df["is_taker"] = new_df["is_taker"].astype(bool)
    new_df["price"] = new_df["price"].astype("float64")
    new_df["shares"] = new_df["shares"].astype("float64").round(2)
    new_df["fill_index"] = new_df["fill_index"].fillna(0).astype("int32")
    # Keep every fill row (same wallet may appear more than once).
    new_df = new_df.sort_values(
        ["timestamp", "transaction_hash", "wallet", "fill_index"]
    ).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    new_df.to_parquet(tmp, index=False)
    tmp.replace(path)
    return len(new_df)


def merge_trades_parquet(path: Path, rows: list[dict[str, Any]]) -> int:
    """Replace legs for txs present in rows; keep other txs untouched."""
    if not rows:
        return 0
    rows = normalize_trade_legs(rows)
    new_df = pd.DataFrame(rows)
    for col in _TRADE_COLS:
        if col not in new_df.columns:
            if col == "fill_index":
                new_df[col] = 0
            else:
                new_df[col] = "" if col in {"transaction_hash", "wallet"} else False
    new_df = new_df[_TRADE_COLS]
    new_df["timestamp"] = new_df["timestamp"].astype("int64")
    new_df["transaction_hash"] = new_df["transaction_hash"].fillna("").astype(str)
    new_df["wallet"] = new_df["wallet"].astype(str)
    new_df["is_up"] = new_df["is_up"].astype(bool)
    new_df["is_buy"] = new_df["is_buy"].astype(bool)
    new_df["is_taker"] = new_df["is_taker"].astype(bool)
    new_df["price"] = new_df["price"].astype("float64")
    new_df["shares"] = new_df["shares"].astype("float64").round(2)
    new_df["fill_index"] = new_df["fill_index"].fillna(0).astype("int32")

    if path.is_file():
        try:
            old = pd.read_parquet(path)
        except Exception:
            old = pd.DataFrame(columns=_TRADE_COLS)
    else:
        old = pd.DataFrame(columns=_TRADE_COLS)

    if not old.empty and "is_up" not in old.columns:
        old = pd.DataFrame(columns=_TRADE_COLS)

    if not old.empty:
        for col in _TRADE_COLS:
            if col not in old.columns:
                old[col] = 0 if col == "fill_index" else (
                    "" if col in {"transaction_hash", "wallet"} else False
                )
        old = old[_TRADE_COLS]
        old["transaction_hash"] = old["transaction_hash"].fillna("").astype(str)

    before = len(old)
    replace_txs = set(
        new_df["transaction_hash"].astype(str).str.lower().tolist()
    ) - {""}
    if replace_txs and not old.empty:
        old_tx = old["transaction_hash"].astype(str).str.lower()
        old = old.loc[~old_tx.isin(replace_txs)].copy()
    merged = pd.concat([old, new_df], ignore_index=True)
    merged = merged.sort_values(
        ["timestamp", "transaction_hash", "wallet", "fill_index"]
    ).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    merged.to_parquet(tmp, index=False)
    tmp.replace(path)
    return max(0, len(merged) - before)


async def backfill_trades_for_market_dir(market_dir: Path) -> int:
    """
    Fetch Data API trades for this market window and rewrite trades.parquet.
    Returns written row count (0 if skipped/failed).
    """
    meta = _meta(market_dir)
    if not meta:
        return 0
    cid = str(meta.get("condition_id") or "").strip()
    if not cid:
        return 0
    try:
        start_ms = int(meta.get("start_time") or 0)
        end_ms = int(meta.get("end_time") or 0)
    except (TypeError, ValueError):
        return 0
    if start_ms <= 0 or end_ms <= start_ms:
        return 0

    token_up = str(meta.get("up_token_id") or "").strip() or None
    token_down = str(meta.get("down_token_id") or "").strip() or None
    try:
        rows = await fetch_data_api_trades(
            condition_id=cid,
            token_up=token_up,
            token_down=token_down,
            start_ms=start_ms,
            end_ms=end_ms,
            max_pages=50,
        )
    except Exception as exc:
        logger.warning("Data API trade backfill failed for %s: %s", market_dir.name, exc)
        return 0
    if not rows:
        return 0
    written = write_trades_parquet(market_dir / "trades.parquet", rows)
    if written:
        logger.info(
            "Rewrote %s trades → %s/trades.parquet",
            written,
            market_dir.name,
        )
    return written
