"""Fill missed 1s price/book rows + Binance trades for a local fetch_live market dir."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

BINANCE_HOSTS = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
)


def _meta(market_dir: Path) -> dict[str, Any] | None:
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


def _floor_s(ts: int) -> int:
    return int(ts) - (int(ts) % 1000)


def _read_df(path: Path) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    try:
        return pd.read_parquet(path)
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return pd.DataFrame()


def _write_df(path: Path, df: pd.DataFrame) -> None:
    if df.empty:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.sort_values("timestamp").reset_index(drop=True).to_parquet(tmp, index=False)
    tmp.replace(path)


async def _binance_json(
    http: httpx.AsyncClient, path: str, params: dict[str, Any]
) -> Any:
    last_exc: Exception | None = None
    for base in BINANCE_HOSTS:
        try:
            resp = await http.get(f"{base}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
    raise last_exc or RuntimeError("Binance REST failed")


async def _agg_trades(
    http: httpx.AsyncClient, *, start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor = int(start_ms)
    for _ in range(200):
        batch = await _binance_json(
            http,
            "/api/v3/aggTrades",
            {
                "symbol": "BTCUSDT",
                "startTime": cursor,
                "endTime": int(end_ms),
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
                out.append(
                    {
                        "timestamp": ts,
                        "price": float(item.get("p") or 0),
                        "quantity": float(item.get("q") or 0),
                        "buyer_is_maker": bool(item.get("m")),
                    }
                )
            except (TypeError, ValueError):
                continue
        last_t = int(batch[-1].get("T") or 0)
        if last_t >= end_ms - 1 or len(batch) < 1000:
            break
        cursor = last_t + 1
        if cursor >= end_ms:
            break
    return [r for r in out if start_ms <= int(r["timestamp"]) < end_ms]


async def _klines_1s(
    http: httpx.AsyncClient, *, start_ms: int, end_ms: int
) -> dict[int, float]:
    out: dict[int, float] = {}
    cursor = int(start_ms)
    for _ in range(20):
        batch = await _binance_json(
            http,
            "/api/v3/klines",
            {
                "symbol": "BTCUSDT",
                "interval": "1s",
                "startTime": cursor,
                "endTime": int(end_ms) - 1,
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
                out[open_ms] = float(item[4])
                last_open = open_ms
            except (TypeError, ValueError):
                continue
        if last_open >= end_ms - 1000 or len(batch) < 1000:
            break
        cursor = last_open + 1000
        if cursor >= end_ms:
            break
    return {t: p for t, p in out.items() if start_ms <= t < end_ms}


def _fill_seconds(
    df: pd.DataFrame, *, start_ms: int, end_ms: int, value_col: str, values: dict[int, float]
) -> tuple[pd.DataFrame, int]:
    if "timestamp" not in df.columns:
        df = pd.DataFrame(columns=["timestamp", value_col])
    df = df.copy()
    df["timestamp"] = df["timestamp"].astype("int64")
    df["timestamp"] = df["timestamp"] - (df["timestamp"] % 1000)
    have = set(df["timestamp"].tolist()) if not df.empty else set()
    last = None
    extra: list[dict[str, Any]] = []
    added = 0
    template = {c: 0.0 for c in df.columns if c not in {"timestamp", value_col, "twap"}}
    for ts in range(_floor_s(start_ms), _floor_s(end_ms), 1000):
        if ts in have:
            if value_col in df.columns:
                hit = df.loc[df["timestamp"] == ts, value_col]
                if not hit.empty and pd.notna(hit.iloc[0]):
                    last = float(hit.iloc[0])
            continue
        px = values.get(ts, last)
        if px is None:
            continue
        last = float(px)
        row = {"timestamp": ts, value_col: last, **template}
        extra.append(row)
        added += 1
        have.add(ts)
    if extra:
        df = pd.concat([df, pd.DataFrame(extra)], ignore_index=True)
    return df, added


def _ffill_orderbooks(df: pd.DataFrame, *, start_ms: int, end_ms: int) -> tuple[pd.DataFrame, int]:
    if df.empty or "timestamp" not in df.columns:
        return df, 0
    df = df.copy()
    df["timestamp"] = df["timestamp"].astype("int64")
    df["timestamp"] = df["timestamp"] - (df["timestamp"] % 1000)
    df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    have = set(df["timestamp"].tolist())
    if not have:
        return df, 0
    records = df.to_dict("records")
    by_ts = {int(r["timestamp"]): r for r in records}
    first = records[0]
    last = None
    extra: list[dict[str, Any]] = []
    for ts in range(_floor_s(start_ms), _floor_s(end_ms), 1000):
        if ts in by_ts:
            last = by_ts[ts]
            continue
        src = last or first
        row = dict(src)
        row["timestamp"] = ts
        extra.append(row)
        by_ts[ts] = row
    if extra:
        df = pd.concat([df, pd.DataFrame(extra)], ignore_index=True)
    return df, len(extra)


async def repair_binance_for_market_dir(
    market_dir: Path,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict[str, int]:
    """Backfill Binance trades + 1s BTC price from Binance REST only."""
    meta = _meta(market_dir) or {}
    try:
        start = int(start_ms if start_ms is not None else meta.get("start_time") or 0)
        end = int(end_ms if end_ms is not None else meta.get("end_time") or 0)
    except (TypeError, ValueError):
        return {}
    if start <= 0 or end <= start:
        return {}

    filled: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=8.0)) as http:
        klines = await _klines_1s(http, start_ms=start, end_ms=end)
        try:
            incoming = await _agg_trades(http, start_ms=start, end_ms=end)
            path = market_dir / "binance_trades.parquet"
            old = _read_df(path)
            new = pd.DataFrame(incoming)
            if not new.empty:
                if old.empty:
                    merged = new
                else:
                    merged = pd.concat([old, new], ignore_index=True)
                if "timestamp" in merged.columns:
                    merged = merged.drop_duplicates(
                        subset=[
                            c
                            for c in ("timestamp", "price", "quantity", "buyer_is_maker")
                            if c in merged.columns
                        ],
                        keep="last",
                    )
                filled["binance_trades.parquet"] = max(0, len(merged) - len(old))
                _write_df(path, merged)
        except Exception as exc:
            logger.warning("Binance trade repair failed for %s: %s", market_dir.name, exc)

        if klines:
            px_path = market_dir / "binance_price_orderbook.parquet"
            px, n = _fill_seconds(
                _read_df(px_path),
                start_ms=start,
                end_ms=end,
                value_col="Binance_BTC",
                values=klines,
            )
            _write_df(px_path, px)
            filled["binance_price_orderbook.parquet"] = n

    if meta:
        meta["repair_filled"] = {**(meta.get("repair_filled") or {}), **filled}
        _write_meta(market_dir, meta)
    return filled


async def repair_series_for_market_dir(market_dir: Path) -> dict[str, int]:
    """Backfill Binance trades + 1s price/book holes. Returns per-file added counts."""
    meta = _meta(market_dir)
    if not meta:
        return {}
    try:
        start_ms = int(meta.get("start_time") or 0)
        end_ms = int(meta.get("end_time") or 0)
    except (TypeError, ValueError):
        return {}
    if start_ms <= 0 or end_ms <= start_ms:
        return {}

    filled: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=8.0)) as http:
        klines = await _klines_1s(http, start_ms=start_ms, end_ms=end_ms)
        try:
            incoming = await _agg_trades(http, start_ms=start_ms, end_ms=end_ms)
            path = market_dir / "binance_trades.parquet"
            old = _read_df(path)
            new = pd.DataFrame(incoming)
            if not new.empty:
                if old.empty:
                    merged = new
                else:
                    merged = pd.concat([old, new], ignore_index=True)
                if "timestamp" in merged.columns:
                    merged = merged.drop_duplicates(
                        subset=[c for c in ("timestamp", "price", "quantity", "buyer_is_maker") if c in merged.columns],
                        keep="last",
                    )
                filled["binance_trades.parquet"] = max(0, len(merged) - len(old))
                _write_df(path, merged)
        except Exception as exc:
            logger.warning("Binance trade repair failed for %s: %s", market_dir.name, exc)

        if klines:
            px_path = market_dir / "binance_price_orderbook.parquet"
            px, n = _fill_seconds(
                _read_df(px_path),
                start_ms=start_ms,
                end_ms=end_ms,
                value_col="Binance_BTC",
                values=klines,
            )
            _write_df(px_path, px)
            filled["binance_price_orderbook.parquet"] = n

            # Prefer pm_chainlink_price when present — do not fill/overwrite it with Binance.
            from app.core.live_dataset import PM_CHAINLINK_FILE, resolve_chainlink_path

            preferred_cl = resolve_chainlink_path(market_dir)
            if preferred_cl is None or preferred_cl.name != PM_CHAINLINK_FILE:
                cl_path = market_dir / "chainlink_price.parquet"
                cl, n = _fill_seconds(
                    _read_df(cl_path),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    value_col="Chainlink_BTC",
                    values=klines,
                )
                if not cl.empty and "Chainlink_BTC" in cl.columns:
                    cl = cl.sort_values("timestamp")
                    if "twap" not in cl.columns:
                        cl["twap"] = pd.NA
                    spots = cl[["timestamp", "Chainlink_BTC"]].dropna()
                    for i, row in cl.iterrows():
                        if pd.notna(row.get("twap")):
                            continue
                        ts = int(row["timestamp"])
                        win = spots[(spots["timestamp"] > ts - 30_000) & (spots["timestamp"] <= ts)]
                        if not win.empty:
                            cl.at[i, "twap"] = float(win["Chainlink_BTC"].mean())
                _write_df(cl_path, cl)
                filled["chainlink_price.parquet"] = n

    # Live orderbooks only — never rewrite pm_orderbooks.parquet.
    from app.core.live_dataset import PM_ORDERBOOKS_FILE, resolve_orderbooks_path

    preferred_ob = resolve_orderbooks_path(market_dir)
    if preferred_ob is None or preferred_ob.name != PM_ORDERBOOKS_FILE:
        ob_path = market_dir / "orderbooks.parquet"
        ob, n = _ffill_orderbooks(_read_df(ob_path), start_ms=start_ms, end_ms=end_ms)
        _write_df(ob_path, ob)
        filled["orderbooks.parquet"] = n

    if meta is not None:
        meta["repair_filled"] = {**(meta.get("repair_filled") or {}), **filled}
        _write_meta(market_dir, meta)
    return filled
