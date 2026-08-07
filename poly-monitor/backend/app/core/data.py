"""Load historical market frames from fetch_real training/features."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from app.core.config import settings

SPLITS = ("train", "validation", "test")

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
    return None


def list_markets(split: str, *, limit: int | None = None) -> list[dict[str, Any]]:
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
    split = split or find_split(mid)
    if not split:
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
    split = split or find_split(mid)
    if not split:
        raise FileNotFoundError(f"Market not found: {mid}")

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


def series_for_chart(df: pd.DataFrame, *, max_points: int = 300) -> list[dict[str, Any]]:
    if df.empty:
        return []
    step = max(1, len(df) // max_points)
    rows = df.iloc[::step]
    out: list[dict[str, Any]] = []
    for _, r in rows.iterrows():
        out.append(
            {
                "t": int(r["timestamp"]),
                "btc": float(r["btc_price"]) if "btc_price" in df.columns and pd.notna(r.get("btc_price")) else None,
                "up": float(r["up_price"]) if pd.notna(r.get("up_price")) else None,
                "down": float(r["down_price"]) if pd.notna(r.get("down_price")) else None,
            }
        )
    return out


def book_at(df: pd.DataFrame, timestamp: int | None = None) -> dict[str, Any]:
    if df.empty:
        return {"bids": [], "asks": [], "timestamp": None}
    if timestamp is None:
        row = df.iloc[-1]
    else:
        idx = (df["timestamp"] - timestamp).abs().idxmin()
        row = df.loc[idx]

    def levels(side: str, kind: str) -> list[dict[str, float]]:
        # Synthetic ladder from distance buckets when present
        suffixes = ("0_1", "1_3", "3_7", "7_15", "15_30", "30_plus")
        mid = float(row.get(f"{side}_price") or 0.5)
        out: list[dict[str, float]] = []
        # cents offsets midpoints for display
        offsets = (0.5, 2.0, 5.0, 11.0, 22.5, 40.0)
        for suf, off in zip(suffixes, offsets):
            col = f"{side}_{kind}_{suf}"
            if col not in df.columns:
                continue
            size = float(row[col]) if pd.notna(row[col]) else 0.0
            if size <= 0:
                continue
            cents = off / 100.0
            price = mid - cents if kind == "bid" else mid + cents
            price = max(0.01, min(0.99, price))
            out.append({"price": round(price, 3), "size": size})
        return out

    top_bid = row.get("up_bid_price")
    top_ask = row.get("up_ask_price")
    return {
        "timestamp": int(row["timestamp"]),
        "up": {
            "best_bid": float(top_bid) if pd.notna(top_bid) else None,
            "best_ask": float(top_ask) if pd.notna(top_ask) else None,
            "bids": levels("up", "bid"),
            "asks": levels("up", "ask"),
        },
        "down": {
            "best_bid": float(row["down_bid_price"]) if "down_bid_price" in df.columns and pd.notna(row.get("down_bid_price")) else None,
            "best_ask": float(row["down_ask_price"]) if "down_ask_price" in df.columns and pd.notna(row.get("down_ask_price")) else None,
            "bids": levels("down", "bid"),
            "asks": levels("down", "ask"),
        },
        "up_price": float(row["up_price"]) if pd.notna(row.get("up_price")) else None,
        "down_price": float(row["down_price"]) if pd.notna(row.get("down_price")) else None,
    }
