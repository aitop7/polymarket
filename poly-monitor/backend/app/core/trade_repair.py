"""Repair sparse local trades.parquet from Polymarket Data API."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

DATA_API_URL = "https://data-api.polymarket.com"
_TRADE_COLS = ["timestamp", "wallet", "token", "side", "price", "shares"]


def _meta(market_dir: Path) -> dict[str, Any] | None:
    path = market_dir / "meta.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _row_from_api(
    trade: dict[str, Any],
    *,
    token_up: str | None,
    token_down: str | None,
    start_ms: int,
    end_ms: int,
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
    is_down: bool | None = None
    if token_up and asset == str(token_up):
        is_down = False
    elif token_down and asset == str(token_down):
        is_down = True
    else:
        outcome = str(trade.get("outcome") or "").strip().lower()
        if outcome in {"up", "yes"}:
            is_down = False
        elif outcome in {"down", "no"}:
            is_down = True
        else:
            try:
                is_down = int(trade.get("outcomeIndex")) == 1
            except (TypeError, ValueError):
                return None
    if is_down is None:
        return None

    side_raw = str(trade.get("side") or "BUY").upper()
    is_sell = side_raw in {"SELL", "S"}
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
    )
    shares = max(0, min(int(round(size)), 2**32 - 1))
    return {
        "timestamp": ts,
        "wallet": wallet,
        "token": bool(is_down),
        "side": bool(is_sell),
        "price": float(price),
        "shares": shares,
    }


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
    out: list[dict[str, Any]] = []
    offset = 0
    async with httpx.AsyncClient(
        base_url=DATA_API_URL,
        timeout=httpx.Timeout(25.0, connect=8.0),
    ) as http:
        for _ in range(max_pages):
            resp = await http.get(
                "/trades",
                params={"market": condition_id, "limit": 500, "offset": offset},
            )
            resp.raise_for_status()
            raw = resp.json()
            batch = raw if isinstance(raw, list) else []
            if not batch:
                break
            oldest: int | None = None
            for item in batch:
                if not isinstance(item, dict):
                    continue
                try:
                    raw_ts = int(item.get("timestamp") or 0)
                except (TypeError, ValueError):
                    raw_ts = 0
                if raw_ts > 0:
                    if raw_ts < 10_000_000_000:
                        raw_ts *= 1000
                    oldest = raw_ts if oldest is None else min(oldest, raw_ts)
                row = _row_from_api(
                    item,
                    token_up=token_up,
                    token_down=token_down,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
                if row is not None:
                    out.append(row)
            if oldest is not None and start_ms and oldest < int(start_ms) - 300_000:
                break
            if len(batch) < 500:
                break
            offset += 500
    return out


def merge_trades_parquet(path: Path, rows: list[dict[str, Any]]) -> int:
    """Upsert rows into trades.parquet; return number of newly added rows."""
    if not rows:
        return 0
    new_df = pd.DataFrame(rows)
    for col in _TRADE_COLS:
        if col not in new_df.columns:
            new_df[col] = None
    new_df = new_df[_TRADE_COLS]
    new_df["timestamp"] = new_df["timestamp"].astype("int64")
    new_df["wallet"] = new_df["wallet"].astype(str)
    new_df["token"] = new_df["token"].astype(bool)
    new_df["side"] = new_df["side"].astype(bool)
    new_df["price"] = new_df["price"].astype("float64")
    new_df["shares"] = new_df["shares"].astype("int64")

    if path.is_file():
        try:
            old = pd.read_parquet(path)
        except Exception:
            old = pd.DataFrame(columns=_TRADE_COLS)
    else:
        old = pd.DataFrame(columns=_TRADE_COLS)

    if not old.empty:
        for col in _TRADE_COLS:
            if col not in old.columns:
                old[col] = None
        old = old[_TRADE_COLS]

    before = len(old)
    merged = pd.concat([old, new_df], ignore_index=True)
    merged = merged.drop_duplicates(
        subset=["timestamp", "wallet", "token", "side", "price", "shares"],
        keep="last",
    )
    merged = merged.sort_values("timestamp").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    merged.to_parquet(tmp, index=False)
    tmp.replace(path)
    return max(0, len(merged) - before)


async def backfill_trades_for_market_dir(market_dir: Path) -> int:
    """
    Fetch Data API trades for this market window and merge into trades.parquet.
    Returns newly added row count.
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
        )
    except Exception as exc:
        logger.warning("Data API trade backfill failed for %s: %s", market_dir.name, exc)
        return 0
    if not rows:
        return 0
    added = merge_trades_parquet(market_dir / "trades.parquet", rows)
    if added:
        logger.info(
            "Backfilled %s Data API trades → %s/trades.parquet (api_rows=%s)",
            added,
            market_dir.name,
            len(rows),
        )
    return added
