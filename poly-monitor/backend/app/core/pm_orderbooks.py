"""Build pm_orderbooks.parquet from PMData poly_l2 (0.5s grid, ORDERBOOK schema)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.core.live_dataset import find_live_market_dir
from app.core.pmdata_client import download_poly_l2, pmdata_enabled

SLOT_MS = 500
PM_ORDERBOOKS_FILE = "pm_orderbooks.parquet"

BUCKET_SUFFIXES = ("0_1", "1_3", "3_7", "7_15", "15_30", "30_plus")
DISTANCE_BUCKETS: list[tuple[int, int | None]] = [
    (0, 1),
    (1, 3),
    (3, 7),
    (7, 15),
    (15, 30),
    (30, None),
]

ORDERBOOK_COLUMNS = [
    "timestamp",
    "up_price",
    "down_price",
    "up_bid_price",
    "up_bid_shares",
    "up_ask_price",
    "up_ask_shares",
    "down_bid_price",
    "down_bid_shares",
    "down_ask_price",
    "down_ask_shares",
    *[f"up_ask_{s}" for s in BUCKET_SUFFIXES],
    *[f"up_bid_{s}" for s in BUCKET_SUFFIXES],
    *[f"down_ask_{s}" for s in BUCKET_SUFFIXES],
    *[f"down_bid_{s}" for s in BUCKET_SUFFIXES],
]


def _shares_u32(size: float | None) -> int:
    if size is None:
        return 0
    v = int(round(float(size)))
    return max(0, min(v, 2**32 - 1))


def _f32(price: float | None) -> float | None:
    if price is None:
        return None
    try:
        v = float(price)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    return v


def _best(levels: list[dict[str, float]], *, reverse: bool) -> dict[str, float] | None:
    if not levels:
        return None
    return sorted(levels, key=lambda x: x["price"], reverse=reverse)[0]


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return bid if bid is not None else ask


def _bucket_shares(
    levels: list[dict[str, float]],
    *,
    ref_price: float,
    side: str,
) -> dict[str, int]:
    ref_c = float(ref_price) * 100.0
    totals = {suffix: 0.0 for suffix in BUCKET_SUFFIXES}
    for level in levels:
        px = float(level["price"])
        sz = float(level["size"])
        c = px * 100.0
        if side == "ask":
            dist = c - ref_c
            if dist < 0:
                continue
        else:
            dist = ref_c - c
            if dist < 0:
                continue
        for (lo, hi), suffix in zip(DISTANCE_BUCKETS, BUCKET_SUFFIXES):
            if hi is None:
                if dist >= lo:
                    totals[suffix] += sz
                    break
            elif lo <= dist < hi:
                totals[suffix] += sz
                break
    return {k: _shares_u32(v) for k, v in totals.items()}


def side_flat_fields(
    prefix: str,
    *,
    bids: list[dict[str, float]],
    asks: list[dict[str, float]],
    traded_price: float | None,
) -> dict[str, Any]:
    best_bid = _best(bids, reverse=True)
    best_ask = _best(asks, reverse=False)
    ref = traded_price
    if ref is None:
        ref = _mid(
            best_bid["price"] if best_bid else None,
            best_ask["price"] if best_ask else None,
        )
    if ref is None:
        ref = 0.5

    out: dict[str, Any] = {
        f"{prefix}_price": _f32(traded_price),
        f"{prefix}_bid_price": _f32(best_bid["price"] if best_bid else None),
        f"{prefix}_bid_shares": _shares_u32(best_bid["size"] if best_bid else 0),
        f"{prefix}_ask_price": _f32(best_ask["price"] if best_ask else None),
        f"{prefix}_ask_shares": _shares_u32(best_ask["size"] if best_ask else 0),
    }
    ask_buckets = _bucket_shares(asks, ref_price=ref, side="ask")
    bid_buckets = _bucket_shares(bids, ref_price=ref, side="bid")
    for suffix in BUCKET_SUFFIXES:
        out[f"{prefix}_ask_{suffix}"] = ask_buckets[suffix]
        out[f"{prefix}_bid_{suffix}"] = bid_buckets[suffix]
    return out


def build_orderbook_row(
    *,
    timestamp_ms: int,
    up_bids: list[dict[str, float]] | None = None,
    up_asks: list[dict[str, float]] | None = None,
    down_bids: list[dict[str, float]] | None = None,
    down_asks: list[dict[str, float]] | None = None,
    up_price: float | None = None,
    down_price: float | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"timestamp": int(timestamp_ms)}
    row.update(
        side_flat_fields(
            "up",
            bids=up_bids or [],
            asks=up_asks or [],
            traded_price=up_price,
        )
    )
    row.update(
        side_flat_fields(
            "down",
            bids=down_bids or [],
            asks=down_asks or [],
            traded_price=down_price,
        )
    )
    for col in ORDERBOOK_COLUMNS:
        if col not in row or row[col] is None:
            if col == "timestamp":
                continue
            if col.endswith("_shares") or any(col.endswith(f"_{s}") for s in BUCKET_SUFFIXES):
                row[col] = 0
            else:
                row[col] = None
        elif col.endswith("_shares") or any(col.endswith(f"_{s}") for s in BUCKET_SUFFIXES):
            row[col] = int(row[col] or 0)
    return {c: row.get(c) for c in ORDERBOOK_COLUMNS}


def _read_meta(market_dir: Path) -> dict[str, Any]:
    path = market_dir / "meta.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"invalid meta.json: {path}")
    return raw


def _to_ms(ts: Any) -> int | None:
    """Normalize PMData / pandas timestamps to epoch milliseconds."""
    if ts is None or (isinstance(ts, float) and not np.isfinite(ts)):
        return None
    if isinstance(ts, pd.Timestamp):
        if pd.isna(ts):
            return None
        # Timestamp.value is always nanoseconds since epoch.
        return int(ts.value // 1_000_000)
    if isinstance(ts, np.datetime64):
        if np.isnat(ts):
            return None
        return int(ts.astype("datetime64[ms]").astype(np.int64))
    if hasattr(ts, "timestamp") and callable(ts.timestamp):
        try:
            return int(round(float(ts.timestamp()) * 1000.0))
        except (OSError, OverflowError, ValueError, TypeError):
            pass
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return None
    # ns (pandas int view / Timestamp.value-style)
    if v >= 10_000_000_000_000_000:  # ~year 2286 in µs → treat as ns
        return v // 1_000_000
    # PMData integer micros; live meta uses milliseconds.
    if v >= 10_000_000_000_000:  # >= ~year 2286 in ms → treat as µs
        return v // 1000
    if v >= 1_000_000_000_000:  # ms
        return v
    if v >= 1_000_000_000:  # seconds
        return v * 1000
    return None


def _series_to_ms(series: pd.Series) -> pd.Series:
    """Vectorized epoch-ms conversion for a timestamp column."""
    if pd.api.types.is_datetime64_any_dtype(series):
        # datetime64[ns/us/ms] → int64 in that unit; normalize via ns.
        as_ns = series.astype("datetime64[ns]").astype("int64")
        out = (as_ns // 1_000_000).astype("Int64")
        # NaT becomes very negative int; mask those.
        nat = series.isna()
        if nat.any():
            out = out.mask(nat)
        return out
    return series.map(_to_ms).astype("Int64")


def _as_float_list(raw: Any) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, np.ndarray):
        return [float(x) for x in raw.tolist() if x is not None and np.isfinite(float(x))]
    if isinstance(raw, (list, tuple)):
        out: list[float] = []
        for x in raw:
            try:
                fx = float(x)
            except (TypeError, ValueError):
                continue
            if np.isfinite(fx):
                out.append(fx)
        return out
    # pandas / pyarrow scalar list
    try:
        return _as_float_list(list(raw))
    except Exception:
        return []


def _levels_from_arrays(prices: Any, sizes: Any) -> list[dict[str, float]]:
    px = _as_float_list(prices)
    sz = _as_float_list(sizes)
    n = min(len(px), len(sz))
    out: list[dict[str, float]] = []
    for i in range(n):
        if sz[i] <= 0:
            continue
        if not (0.0 < px[i] < 1.0):
            continue
        out.append({"price": px[i], "size": sz[i]})
    return out


def _book_from_levels(
    bids: list[dict[str, float]], asks: list[dict[str, float]]
) -> tuple[dict[float, float], dict[float, float]]:
    bid_map = {round(float(l["price"]), 4): float(l["size"]) for l in bids}
    ask_map = {round(float(l["price"]), 4): float(l["size"]) for l in asks}
    return bid_map, ask_map


def _maps_to_levels(
    bid_map: dict[float, float], ask_map: dict[float, float]
) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    bids = [{"price": p, "size": s} for p, s in bid_map.items() if s > 0]
    asks = [{"price": p, "size": s} for p, s in ask_map.items() if s > 0]
    return bids, asks


def _complement_book(
    bids: list[dict[str, float]], asks: list[dict[str, float]]
) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    """DOWN ≈ complement of UP: down_bid≈1-up_ask, down_ask≈1-up_bid."""
    down_bids = [{"price": round(1.0 - a["price"], 4), "size": a["size"]} for a in asks]
    down_asks = [{"price": round(1.0 - b["price"], 4), "size": b["size"]} for b in bids]
    down_bids = [l for l in down_bids if 0.0 < l["price"] < 1.0 and l["size"] > 0]
    down_asks = [l for l in down_asks if 0.0 < l["price"] < 1.0 and l["size"] > 0]
    return down_bids, down_asks


def _asset_key(row: pd.Series) -> str | None:
    for col in (
        "asset_id",
        "token_id",
        "asset",
        "outcome_id",
        "outcome",
        "side",
        "token",
    ):
        if col in row.index and pd.notna(row[col]):
            return str(row[col]).strip()
    return None


def _classify_asset(
    key: str | None,
    *,
    up_token: str | None,
    down_token: str | None,
) -> str:
    """Return 'up', 'down', or 'up' default."""
    if not key:
        return "up"
    k = key.lower()
    if up_token and (key == up_token or k == str(up_token).lower()):
        return "up"
    if down_token and (key == down_token or k == str(down_token).lower()):
        return "down"
    if k in {"up", "yes", "long", "0", "buy_up"}:
        return "up"
    if k in {"down", "no", "short", "1", "buy_down"}:
        return "down"
    return "up"


class _SideBook:
    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_mid: float | None = None

    def apply_snapshot(self, bids: list[dict[str, float]], asks: list[dict[str, float]]) -> None:
        self.bids, self.asks = _book_from_levels(bids, asks)
        bb = _best(bids, reverse=True)
        ba = _best(asks, reverse=False)
        self.last_mid = _mid(
            bb["price"] if bb else None,
            ba["price"] if ba else None,
        )

    def apply_price_change(self, *, side: str, price: float, size: float) -> None:
        px = round(float(price), 4)
        book = self.bids if side == "bid" else self.asks
        if size <= 0:
            book.pop(px, None)
        else:
            book[px] = float(size)
        bb = max(self.bids) if self.bids else None
        ba = min(self.asks) if self.asks else None
        self.last_mid = _mid(bb, ba)

    def levels(self) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
        return _maps_to_levels(self.bids, self.asks)


def _parse_event_type(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if "price" in s and "change" in s:
        return "price_change"
    if s in {"book", "snapshot", "b"}:
        return "book"
    return s


def _apply_row(books: dict[str, _SideBook], row: pd.Series, which: str) -> None:
    book = books[which]
    et = _parse_event_type(row.get("event_type") if "event_type" in row.index else None)

    if et == "book" or (
        "ask_prices" in row.index
        and "bid_prices" in row.index
        and et not in {"price_change", "market_resolved"}
    ):
        asks = _levels_from_arrays(
            row["ask_prices"] if "ask_prices" in row.index else None,
            row["ask_sizes"] if "ask_sizes" in row.index else None,
        )
        bids = _levels_from_arrays(
            row["bid_prices"] if "bid_prices" in row.index else None,
            row["bid_sizes"] if "bid_sizes" in row.index else None,
        )
        if asks or bids:
            book.apply_snapshot(bids, asks)
            return

    # price_change style: PMData uses pc_side/pc_price/pc_size (BUY|SELL).
    side_raw = None
    for col in ("pc_side", "side", "book_side", "change_side"):
        if col in row.index and pd.notna(row[col]):
            side_raw = str(row[col]).strip().lower()
            break
    price = None
    for col in ("pc_price", "price", "change_price", "px"):
        if col in row.index and pd.notna(row[col]):
            try:
                price = float(row[col])
            except (TypeError, ValueError):
                price = None
            break
    size = None
    for col in ("pc_size", "size", "change_size", "quantity", "qty"):
        if col in row.index and pd.notna(row[col]):
            try:
                size = float(row[col])
            except (TypeError, ValueError):
                size = None
            break
    if side_raw and price is not None and size is not None:
        if side_raw.startswith("b") or side_raw == "buy":
            side = "bid"
        elif side_raw.startswith("a") or side_raw == "sell" or side_raw.startswith("s"):
            side = "ask"
        else:
            side = "ask"
        book.apply_price_change(side=side, price=price, size=size)


def _pc_side(side_raw: Any) -> str | None:
    if side_raw is None or (isinstance(side_raw, float) and not np.isfinite(side_raw)):
        return None
    s = str(side_raw).strip().lower()
    if not s or s == "nan":
        return None
    if s.startswith("b") or s == "buy":
        return "bid"
    if s.startswith("a") or s == "sell" or s.startswith("s"):
        return "ask"
    return "ask"


def _build_event_stream(
    df: pd.DataFrame,
    *,
    up_token: str | None,
    down_token: str | None,
) -> tuple[list[tuple[int, str, Any]], bool, bool, str | None]:
    """
    Pre-parse PMData rows into a compact event list for fast grid replay.

    Event payloads:
      ("book", which, bids, asks)
      ("pc", which, side, price, size)
    """
    ts = df["_ts_ms"].to_numpy(dtype=np.int64, copy=False)
    n = len(df)
    has_et = "event_type" in df.columns
    has_asset = any(
        c in df.columns
        for c in ("asset_id", "token_id", "asset", "outcome_id", "outcome", "side", "token")
    )
    ask_prices = df["ask_prices"].to_numpy(copy=False) if "ask_prices" in df.columns else None
    ask_sizes = df["ask_sizes"].to_numpy(copy=False) if "ask_sizes" in df.columns else None
    bid_prices = df["bid_prices"].to_numpy(copy=False) if "bid_prices" in df.columns else None
    bid_sizes = df["bid_sizes"].to_numpy(copy=False) if "bid_sizes" in df.columns else None
    pc_side = df["pc_side"].to_numpy(copy=False) if "pc_side" in df.columns else None
    pc_price = df["pc_price"].to_numpy(copy=False) if "pc_price" in df.columns else None
    pc_size = df["pc_size"].to_numpy(copy=False) if "pc_size" in df.columns else None
    et_arr = df["event_type"].to_numpy(copy=False) if has_et else None

    events: list[tuple[int, str, Any]] = []
    saw_up = False
    saw_down = False

    for i in range(n):
        which = "up"
        if has_asset:
            key = _asset_key(df.iloc[i])
            which = _classify_asset(key, up_token=up_token, down_token=down_token)
        if which == "down":
            saw_down = True
        else:
            saw_up = True

        et = _parse_event_type(et_arr[i] if et_arr is not None else None)
        if et == "market_resolved":
            continue

        t_ms = int(ts[i])
        if et == "price_change" or (
            et != "book"
            and pc_price is not None
            and pc_side is not None
            and pc_size is not None
            and pd.notna(pc_price[i])
            and pd.notna(pc_side[i])
        ):
            side = _pc_side(pc_side[i] if pc_side is not None else None)
            try:
                price = float(pc_price[i]) if pc_price is not None else float("nan")
                size = float(pc_size[i]) if pc_size is not None else float("nan")
            except (TypeError, ValueError):
                continue
            if side is None or not np.isfinite(price) or not np.isfinite(size):
                continue
            events.append((t_ms, "pc", (which, side, price, size)))
            continue

        if ask_prices is not None and bid_prices is not None:
            asks = _levels_from_arrays(
                ask_prices[i],
                ask_sizes[i] if ask_sizes is not None else None,
            )
            bids = _levels_from_arrays(
                bid_prices[i],
                bid_sizes[i] if bid_sizes is not None else None,
            )
            if asks or bids:
                events.append((t_ms, "book", (which, bids, asks)))

    warning = None
    if not saw_down:
        warning = (
            "PMData stream had no DOWN asset id — DOWN book synthesized "
            "as complement of UP (BBO/depth mirrored)."
        )
    return events, saw_up, saw_down, warning


def generate_pm_orderbooks_for_market(
    market_id: str,
    *,
    force_download: bool = False,
    slot_ms: int = SLOT_MS,
) -> dict[str, Any]:
    if not pmdata_enabled():
        raise RuntimeError("PMDATA_API_KEY is not configured")

    mid = str(market_id).strip()
    market_dir = find_live_market_dir(mid)
    if market_dir is None:
        raise FileNotFoundError(f"Live market not found under FETCH_LIVE_DATA_DIR: {mid}")

    meta = _read_meta(market_dir)
    slug = str(meta.get("slug") or "").strip()
    if not slug:
        raise RuntimeError(f"meta.json missing slug for market {mid}")

    try:
        start_ms = int(meta.get("start_time") or 0)
        end_ms = int(meta.get("end_time") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"meta.json missing start/end for market {mid}") from exc
    if start_ms <= 0 or end_ms <= start_ms:
        raise RuntimeError(f"invalid market window for {mid}: {start_ms}-{end_ms}")

    up_token = str(meta.get("up_token_id") or "").strip() or None
    down_token = str(meta.get("down_token_id") or "").strip() or None

    raw = download_poly_l2(slug, force=force_download)
    if raw.empty:
        raise RuntimeError(f"PMData poly_l2 empty for slug={slug}")

    # Sort by timestamp
    ts_col = "timestamp" if "timestamp" in raw.columns else None
    if ts_col is None and "local_timestamp" in raw.columns:
        ts_col = "local_timestamp"
    if ts_col is None:
        raise RuntimeError(f"PMData file missing timestamp columns: {list(raw.columns)}")

    df = raw.copy()
    df["_ts_ms"] = _series_to_ms(df[ts_col])
    df = df.dropna(subset=["_ts_ms"]).sort_values("_ts_ms", kind="mergesort").reset_index(drop=True)
    if df.empty:
        raise RuntimeError(f"PMData poly_l2 had no usable timestamps for slug={slug}")

    # Drop events after market end — not needed for the 0.5s grid.
    ts_all = df["_ts_ms"].to_numpy(dtype=np.int64, copy=False)
    keep_n = int(np.searchsorted(ts_all, end_ms, side="right"))
    if keep_n < len(df):
        df = df.iloc[:keep_n].reset_index(drop=True)

    events, saw_up_asset, saw_down_asset, warning = _build_event_stream(
        df, up_token=up_token, down_token=down_token
    )
    if not events and df.empty:
        raise RuntimeError(f"PMData poly_l2 had no usable timestamps for slug={slug}")

    books = {"up": _SideBook(), "down": _SideBook()}

    slot = max(100, int(slot_ms))
    # Align grid to slot boundaries within window.
    t0 = start_ms - (start_ms % slot)
    if t0 < start_ms:
        t0 += slot
    grid = list(range(t0, end_ms + 1, slot))
    if not grid:
        grid = [start_ms]

    rows_out: list[dict[str, Any]] = []
    ev_i = 0
    n_events = len(events)

    for t in grid:
        while ev_i < n_events and events[ev_i][0] <= t:
            _, kind, payload = events[ev_i]
            if kind == "book":
                which, bids, asks = payload
                books[which].apply_snapshot(bids, asks)
            else:
                which, side, price, size = payload
                books[which].apply_price_change(side=side, price=price, size=size)
            ev_i += 1

        up_bids, up_asks = books["up"].levels()
        if saw_down_asset:
            down_bids, down_asks = books["down"].levels()
            down_price = books["down"].last_mid
        else:
            # Single-stream file: treat as UP, synthesize complementary DOWN.
            down_bids, down_asks = _complement_book(up_bids, up_asks)
            up_mid = books["up"].last_mid
            down_price = (1.0 - up_mid) if up_mid is not None else None

        up_price = books["up"].last_mid
        if up_price is None and up_bids and up_asks:
            up_price = _mid(up_bids[0]["price"] if up_bids else None, up_asks[0]["price"] if up_asks else None)

        rows_out.append(
            build_orderbook_row(
                timestamp_ms=int(t),
                up_bids=up_bids,
                up_asks=up_asks,
                down_bids=down_bids,
                down_asks=down_asks,
                up_price=up_price,
                down_price=down_price,
            )
        )

    if not rows_out:
        raise RuntimeError("no pm_orderbooks rows generated")

    out_df = pd.DataFrame(rows_out, columns=ORDERBOOK_COLUMNS)
    # Ensure dtypes roughly match live orderbooks
    out_df["timestamp"] = out_df["timestamp"].astype("int64")
    for col in ORDERBOOK_COLUMNS:
        if col == "timestamp":
            continue
        if col.endswith("_shares") or any(col.endswith(f"_{s}") for s in BUCKET_SUFFIXES):
            out_df[col] = pd.to_numeric(out_df[col], errors="coerce").fillna(0).astype("uint32")
        else:
            out_df[col] = pd.to_numeric(out_df[col], errors="coerce").astype("float32")

    out_path = market_dir / PM_ORDERBOOKS_FILE
    tmp = out_path.with_suffix(".parquet.tmp")
    out_df.to_parquet(tmp, index=False)
    tmp.replace(out_path)

    return {
        "ok": True,
        "market_id": mid,
        "slug": slug,
        "path": str(out_path),
        "n_rows": int(len(out_df)),
        "slot_ms": slot,
        "source": "pmdata",
        "start_time": start_ms,
        "end_time": end_ms,
        "saw_up_asset": saw_up_asset,
        "saw_down_asset": saw_down_asset,
        "warning": warning,
    }


def has_pm_orderbooks(market_id: str) -> bool:
    d = find_live_market_dir(str(market_id))
    return bool(d and (d / PM_ORDERBOOKS_FILE).is_file())


def list_missing_pm_orderbooks(*, date_et: str | None = None) -> dict[str, Any]:
    """History markets (optionally one ET day) that lack pm_orderbooks.parquet.

    Always returns missing markets sorted oldest → newest (generate from the past).
    Excludes the in-progress live window via filter_history_markets.
    """
    from app.core.live_dataset import TWAP_SPLIT
    from app.core.market_index import (
        build_market_index,
        filter_history_markets,
        list_markets_for_date,
    )

    date = (date_et or "").strip() or None
    if date:
        rows = list_markets_for_date(TWAP_SPLIT, date)
    else:
        rows = filter_history_markets(TWAP_SPLIT, build_market_index(TWAP_SPLIT))

    missing: list[dict[str, Any]] = []
    present = 0
    for r in rows:
        mid = str(r.get("market_id") or "")
        if not mid:
            continue
        d = Path(str(r["dir"])) if r.get("dir") else find_live_market_dir(mid)
        ok = bool(d and (d / PM_ORDERBOOKS_FILE).is_file())
        if ok:
            present += 1
            continue
        missing.append(
            {
                "market_id": mid,
                "slug": None,
                "start_time": int(r.get("start_time") or 0),
                "end_time": int(r.get("end_time") or 0),
                "date_et": r.get("date_et"),
                "time_et": r.get("time_et"),
                "dir": str(d) if d else None,
            }
        )

    # Past → present (do not start from the newest / current-day slot).
    missing.sort(key=lambda r: (int(r.get("start_time") or 0), str(r.get("market_id") or "")))

    for item in missing:
        d = Path(item["dir"]) if item.get("dir") else None
        if d is None:
            continue
        try:
            meta = _read_meta(d)
            item["slug"] = meta.get("slug")
        except Exception:
            pass

    return {
        "date": date,
        "n_total": present + len(missing),
        "n_present": present,
        "n_missing": len(missing),
        "missing": missing,
    }
