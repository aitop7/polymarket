"""Load historical market frames from fetch_real training/features."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from app.core.config import settings
from app.core.live_dataset import (
    TWAP_SPLIT,
    live_market_summary,
    load_live_market_frame,
)

SPLITS = ("train", "validation", "test")
ALL_SPLITS = (*SPLITS, TWAP_SPLIT)

# Feature columns expected by LightGBM (must match fetch_real feature_schema)
FEATURE_COLUMNS = [
    "btc_return_1s",
    "btc_return_5s",
    "btc_return_10s",
    "btc_return_30s",
    "btc_return_60s",
    "btc_momentum_10s",
    "btc_momentum_30s",
    "btc_volatility_10s",
    "btc_volatility_30s",
    "btc_volatility_60s",
    "btc_from_open",
    "btc_high_30s_distance",
    "btc_low_30s_distance",
    "up_spread",
    "down_spread",
    "up_mid",
    "down_mid",
    "up_bid_depth",
    "up_ask_depth",
    "down_bid_depth",
    "down_ask_depth",
    "up_order_imbalance",
    "down_order_imbalance",
    "up_near_bid_ratio",
    "up_near_ask_ratio",
    "down_near_bid_ratio",
    "down_near_ask_ratio",
    "trade_count_5s",
    "buy_volume_5s",
    "sell_volume_5s",
    "up_buy_volume_5s",
    "down_buy_volume_5s",
    "trade_count_10s",
    "buy_volume_10s",
    "sell_volume_10s",
    "up_buy_volume_10s",
    "down_buy_volume_10s",
    "trade_count_30s",
    "buy_volume_30s",
    "sell_volume_30s",
    "up_buy_volume_30s",
    "down_buy_volume_30s",
    "trade_imbalance_5s",
    "trade_imbalance_10s",
    "trade_imbalance_30s",
    "market_probability_gap",
    "up_price_change_5s",
    "up_price_change_10s",
    "up_price_change_30s",
    "btc_market_divergence_10s",
    "btc_market_divergence_30s",
    "elapsed_seconds",
    "remaining_seconds",
    "market_progress",
]


def find_split(market_id: str) -> str | None:
    mid = str(market_id)
    for split in SPLITS:
        if (settings.features_dir / split / f"{mid}.parquet").is_file():
            return split
        if (settings.training_dir / split / f"{mid}.parquet").is_file():
            return split
    from app.core.live_dataset import find_live_market_dir

    if find_live_market_dir(mid) is not None:
        return TWAP_SPLIT
    return None


def list_markets(split: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    if split == TWAP_SPLIT:
        from app.core.market_index import build_market_index

        rows = build_market_index(TWAP_SPLIT)
        if limit is not None:
            rows = rows[: max(0, int(limit))]
        return rows
    if split not in SPLITS:
        raise ValueError(f"Invalid split: {split}")
    feat_dir = settings.features_dir / split
    train_dir = settings.training_dir / split
    ids: list[str] = []
    if feat_dir.is_dir():
        ids = sorted(p.stem for p in feat_dir.glob("*.parquet"))
    elif train_dir.is_dir():
        ids = sorted(p.stem for p in train_dir.glob("*.parquet"))
    if limit is not None:
        ids = ids[: max(0, int(limit))]

    wanted = ("timestamp", "start_time", "end_time", "winner", "btc_open_price")
    out: list[dict[str, Any]] = []
    for mid in ids:
        train_path = train_dir / f"{mid}.parquet"
        feat_path = feat_dir / f"{mid}.parquet"
        path = train_path if train_path.is_file() else feat_path
        if not path.is_file():
            continue
        import pyarrow.parquet as pq

        schema_names = set(pq.read_schema(path).names)
        cols = [c for c in wanted if c in schema_names]
        df = pd.read_parquet(path, columns=cols)
        if df.empty:
            continue
        first, last = df.iloc[0], df.iloc[-1]
        start = int(first["start_time"]) if "start_time" in df.columns else int(first["timestamp"])
        end = int(first["end_time"]) if "end_time" in df.columns else int(last["timestamp"])
        out.append(
            {
                "market_id": mid,
                "split": split,
                "start_time": start,
                "end_time": end,
                "rows": int(pq.ParquetFile(path).metadata.num_rows),
                "winner": int(first["winner"]) if "winner" in df.columns else None,
                "btc_open_price": float(first["btc_open_price"]) if "btc_open_price" in df.columns else None,
                "has_features": feat_path.is_file(),
                "has_training": train_path.is_file(),
            }
        )
    return out


def market_summary(market_id: str, *, split: str | None = None) -> dict[str, Any] | None:
    mid = str(market_id)
    if split == TWAP_SPLIT or (split is None and find_split(mid) == TWAP_SPLIT):
        return live_market_summary(mid)
    split = split or find_split(mid)
    if not split or split == TWAP_SPLIT:
        if split == TWAP_SPLIT:
            return live_market_summary(mid)
        return None
    train_path = settings.training_dir / split / f"{mid}.parquet"
    feat_path = settings.features_dir / split / f"{mid}.parquet"
    path = train_path if train_path.is_file() else feat_path
    if not path.is_file():
        return None

    cols = None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    first = df.iloc[0]
    last = df.iloc[-1]
    start = int(first["start_time"]) if "start_time" in df.columns else int(first["timestamp"])
    end = int(first["end_time"]) if "end_time" in df.columns else int(last["timestamp"])
    winner = int(first["winner"]) if "winner" in df.columns else None
    btc_open = float(first["btc_open_price"]) if "btc_open_price" in df.columns else None
    return {
        "market_id": mid,
        "split": split,
        "start_time": start,
        "end_time": end,
        "rows": int(len(df)),
        "winner": winner,
        "btc_open_price": btc_open,
        "has_features": feat_path.is_file(),
        "has_training": train_path.is_file(),
    }


def load_market_frame(market_id: str, *, split: str | None = None) -> pd.DataFrame:
    """
    Prefer features parquet merged with training BTC/book columns when available.
    """
    mid = str(market_id)
    if split == TWAP_SPLIT or (split is None and find_split(mid) == TWAP_SPLIT):
        from app.ml.live_features import load_live_feature_frame

        return load_live_feature_frame(mid)
    split = split or find_split(mid)
    if not split:
        raise FileNotFoundError(f"Market not found: {mid}")
    if split == TWAP_SPLIT:
        from app.ml.live_features import load_live_feature_frame

        return load_live_feature_frame(mid)

    feat_path = settings.features_dir / split / f"{mid}.parquet"
    train_path = settings.training_dir / split / f"{mid}.parquet"

    if feat_path.is_file() and train_path.is_file():
        feat = pd.read_parquet(feat_path)
        train = pd.read_parquet(train_path)
        keep = [
            c
            for c in (
                "timestamp",
                "btc_price",
                "btc_open_price",
                "btc_close_price",
                "start_time",
                "end_time",
                "up_bid_price",
                "up_ask_price",
                "up_bid_shares",
                "up_ask_shares",
                "down_bid_price",
                "down_ask_price",
                "down_bid_shares",
                "down_ask_shares",
                *[f"up_bid_{s}" for s in ("0_1", "1_3", "3_7", "7_15", "15_30", "30_plus")],
                *[f"up_ask_{s}" for s in ("0_1", "1_3", "3_7", "7_15", "15_30", "30_plus")],
                *[f"down_bid_{s}" for s in ("0_1", "1_3", "3_7", "7_15", "15_30", "30_plus")],
                *[f"down_ask_{s}" for s in ("0_1", "1_3", "3_7", "7_15", "15_30", "30_plus")],
            )
            if c in train.columns
        ]
        merged = feat.merge(train[keep], on="timestamp", how="left", suffixes=("", "_train"))
        return merged.sort_values("timestamp", kind="mergesort").reset_index(drop=True)

    if feat_path.is_file():
        return pd.read_parquet(feat_path).sort_values("timestamp", kind="mergesort").reset_index(drop=True)

    if train_path.is_file():
        return pd.read_parquet(train_path).sort_values("timestamp", kind="mergesort").reset_index(drop=True)

    raise FileNotFoundError(f"No parquet for market {mid} in {split}")


def _chart_up_buy(row: Any) -> float | None:
    """
    Up buy for charts.
    Open placeholders are often up_price=99¢ with an empty or mid-range book — prefer book.
    Near resolution, 99¢/1¢ with an extreme book is real and must be kept.
    """
    from app.core.pricing import _as_float

    lo, hi = 0.02, 0.98
    if isinstance(row, pd.Series):
        up = _as_float(row["up_price"]) if "up_price" in row.index else None
        bid = _as_float(row["up_bid_price"]) if "up_bid_price" in row.index else None
        ask = _as_float(row["up_ask_price"]) if "up_ask_price" in row.index else None
    else:
        up = _as_float(row.get("up_price")) if isinstance(row, dict) else None
        bid = _as_float(row.get("up_bid_price")) if isinstance(row, dict) else None
        ask = _as_float(row.get("up_ask_price")) if isinstance(row, dict) else None

    book_mid: float | None = None
    if bid is not None and ask is not None and bid <= ask:
        book_mid = (bid + ask) / 2.0
    elif ask is not None:
        book_mid = ask
    elif bid is not None:
        book_mid = bid

    if up is not None and lo < up < hi:
        return up
    # Mid-range book wins over extreme placeholder last-trade.
    if book_mid is not None and lo < book_mid < hi:
        return book_mid
    # Settling tape: extreme last-trade confirmed by extreme (or one-sided) book.
    if up is not None and (up <= lo or up >= hi):
        if book_mid is not None and (book_mid <= lo or book_mid >= hi):
            return up
        if bid is not None and (bid <= lo or bid >= hi):
            return up
        if ask is not None and (ask <= lo or ask >= hi):
            return up
        return None
    return book_mid



def series_for_chart(
    df: pd.DataFrame,
    *,
    max_points: int = 600,
    market_id: str | None = None,
) -> list[dict[str, Any]]:
    if df.empty:
        return []
    from app.core.pricing import quotes_from_up_buy
    from app.core.trade_volume import attach_volumes_to_series, volumes_for_market_id
    from app.live.fetch_live_series import (
        break_outcome_jumps,
        resample_series_frame,
        scrub_leading_outcome_extremes,
    )

    # Always 1s grid — even when pm_orderbooks / pm_chainlink are 500ms.
    chart_df = resample_series_frame(df)
    if chart_df.empty:
        return []
    step = max(1, len(chart_df) // max_points)
    rows = chart_df.iloc[::step]
    out: list[dict[str, Any]] = []
    for _, r in rows.iterrows():
        up_buy = _chart_up_buy(r)
        if up_buy is None:
            up_v = down_v = None
        else:
            q = quotes_from_up_buy(up_buy)
            up_v, down_v = q["up_price"], q["down_price"]
        point: dict[str, Any] = {
            "t": int(r["timestamp"]),
            "btc": float(r["btc_price"]) if "btc_price" in chart_df.columns and pd.notna(r.get("btc_price")) else None,
            "up": up_v,
            "down": down_v,
        }
        if "btc_twap_30s" in chart_df.columns and pd.notna(r.get("btc_twap_30s")):
            point["twap"] = float(r["btc_twap_30s"])
        if "btc_chainlink" in chart_df.columns and pd.notna(r.get("btc_chainlink")):
            point["chainlink"] = float(r["btc_chainlink"])
        out.append(point)
    # Same as live: drop open 1¢/99¢ placeholders and break prior-window bleed jumps.
    out = scrub_leading_outcome_extremes(out)
    out = break_outcome_jumps(out)
    # Omit leading/trailing null outcome quotes — Recharts draws null as 0¢.
    i = 0
    while i < len(out) and out[i].get("up") is None and out[i].get("down") is None:
        i += 1
    j = len(out)
    while j > i and out[j - 1].get("up") is None and out[j - 1].get("down") is None:
        j -= 1
    if i or j < len(out):
        out = out[i:j]
    # Live-only: wipe a stuck RTDS tape (identical samples). Do NOT apply this to
    # history — quiet 5m windows often move TWAP/Chainlink by well under $1.
    if market_id is None:
        for key in ("twap", "chainlink"):
            vals = [float(p[key]) for p in out if p.get(key) is not None]
            if len(vals) >= 8 and (max(vals) - min(vals)) < 0.05:
                for p in out:
                    p[key] = None
    if market_id:
        out = attach_volumes_to_series(out, volumes_for_market_id(str(market_id)))
    return out


BUCKET_META = [
    {"suffix": "0_1", "lo_cents": 0, "hi_cents": 1, "mid_cents": 0.5},
    {"suffix": "1_3", "lo_cents": 1, "hi_cents": 3, "mid_cents": 2.0},
    {"suffix": "3_7", "lo_cents": 3, "hi_cents": 7, "mid_cents": 5.0},
    {"suffix": "7_15", "lo_cents": 7, "hi_cents": 15, "mid_cents": 11.0},
    {"suffix": "15_30", "lo_cents": 15, "hi_cents": 30, "mid_cents": 22.5},
    {"suffix": "30_plus", "lo_cents": 30, "hi_cents": None, "mid_cents": 40.0},
]


def _fmt_abs_cents(lo: float, hi: float | None, *, open_high: bool = False, open_low: bool = False) -> str:
    """Format absolute price band in cents with 2-decimal fraction."""
    lo_v = round(max(0.0, min(100.0, lo)), 2)
    if open_high or hi is None:
        return f"{lo_v:.2f}c+"
    hi_v = round(max(0.0, min(100.0, hi)), 2)
    if open_low:
        return f"0.00-{hi_v:.2f}c"
    a, b = (hi_v, lo_v) if lo_v > hi_v else (lo_v, hi_v)
    if abs(a - b) < 1e-9:
        return f"{a:.2f}c"
    return f"{a:.2f}-{b:.2f}c"


def book_at(df: pd.DataFrame, timestamp: int | None = None) -> dict[str, Any]:
    """
    Order book from distance-from-traded-price share buckets.

    Levels are labeled with absolute price ranges (e.g. 49–50¢), mapped from
    stored distance bands relative to the outcome traded price.
    """
    if df.empty:
        return {"timestamp": None, "up": None, "down": None}

    if timestamp is None:
        row = df.iloc[-1]
    else:
        idx = (df["timestamp"] - timestamp).abs().idxmin()
        row = df.loc[idx]

    def side_book(outcome: str) -> dict[str, Any]:
        from app.core.pricing import quotes_from_row

        q = quotes_from_row(row)
        traded = q["up_price"] if outcome == "up" else q["down_price"]
        traded_cents = traded * 100.0
        best_bid = traded - 0.01
        best_ask = traded
        spread = 0.01

        asks: list[dict[str, Any]] = []
        bids: list[dict[str, Any]] = []
        for meta in BUCKET_META:
            ask_col = f"{outcome}_ask_{meta['suffix']}"
            bid_col = f"{outcome}_bid_{meta['suffix']}"
            ask_shares = float(row[ask_col]) if ask_col in df.columns and pd.notna(row.get(ask_col)) else 0.0
            bid_shares = float(row[bid_col]) if bid_col in df.columns and pd.notna(row.get(bid_col)) else 0.0
            mid_off = meta["mid_cents"] / 100.0
            ask_px = max(0.01, min(0.99, traded + mid_off))
            bid_px = max(0.01, min(0.99, traded - mid_off))

            ask_lo_c = max(0.0, min(100.0, traded_cents + meta["lo_cents"]))
            ask_hi_c = (
                None
                if meta["hi_cents"] is None
                else max(0.0, min(100.0, traded_cents + meta["hi_cents"]))
            )
            ask_range = _fmt_abs_cents(ask_lo_c, ask_hi_c, open_high=meta["hi_cents"] is None)

            if meta["hi_cents"] is None:
                bid_hi_c = max(0.0, min(100.0, traded_cents - meta["lo_cents"]))
                bid_lo_c = 0.0
                bid_range = _fmt_abs_cents(bid_lo_c, bid_hi_c, open_low=True)
            else:
                bid_lo_c = max(0.0, min(100.0, traded_cents - meta["hi_cents"]))
                bid_hi_c = max(0.0, min(100.0, traded_cents - meta["lo_cents"]))
                bid_range = _fmt_abs_cents(bid_lo_c, bid_hi_c)

            asks.append(
                {
                    "range": ask_range,
                    "suffix": meta["suffix"],
                    "lo_cents": meta["lo_cents"],
                    "hi_cents": meta["hi_cents"],
                    "price_lo": round(ask_lo_c / 100.0, 4),
                    "price_hi": None if ask_hi_c is None else round(ask_hi_c / 100.0, 4),
                    "shares": ask_shares,
                    "approx_price": round(ask_px, 4),
                    "notional": round(ask_shares * ask_px, 2),
                }
            )
            bids.append(
                {
                    "range": bid_range,
                    "suffix": meta["suffix"],
                    "lo_cents": meta["lo_cents"],
                    "hi_cents": meta["hi_cents"],
                    "price_lo": round(bid_lo_c / 100.0, 4),
                    "price_hi": round(bid_hi_c / 100.0, 4),
                    "shares": bid_shares,
                    "approx_price": round(bid_px, 4),
                    "notional": round(bid_shares * bid_px, 2),
                }
            )

        asks_view = list(reversed(asks))
        bids_view = bids

        ask_total = sum(x["shares"] for x in asks)
        bid_total = sum(x["shares"] for x in bids)

        return {
            "traded_price": traded,
            "best_bid": float(best_bid),
            "best_ask": float(best_ask),
            "spread": float(spread),
            "asks": asks_view,
            "bids": bids_view,
            "ask_shares": ask_total,
            "bid_shares": bid_total,
            "volume_shares": ask_total + bid_total,
        }

    from app.core.pricing import quotes_from_row

    q = quotes_from_row(row)
    return {
        "timestamp": int(row["timestamp"]),
        "mode": "absolute_ranges",
        "note": "Share buckets stored by distance from traded price; labels show absolute ¢ ranges. Down buy = 101¢ − Up buy; sell = buy − 1¢.",
        "up": side_book("up"),
        "down": side_book("down"),
        "up_price": q["up_price"],
        "down_price": q["down_price"],
        "up_buy": q["up_buy"],
        "down_buy": q["down_buy"],
        "up_sell": q["up_sell"],
        "down_sell": q["down_sell"],
    }
