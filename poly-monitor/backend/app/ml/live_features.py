"""Engineer FEATURE_COLUMNS from fetch_live (E:\\DataSets\\poly\\live) market dirs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.core.data import FEATURE_COLUMNS
from app.core.live_dataset import find_live_market_dir, load_live_market_frame

_DEPTH_BANDS = ("0_1", "1_3", "3_7", "7_15", "15_30", "30_plus")
_NEAR_BANDS = ("0_1", "1_3")


def _num(series: pd.Series | None, n: int) -> np.ndarray:
    if series is None:
        return np.full(n, np.nan, dtype=np.float64)
    return pd.to_numeric(series, errors="coerce").to_numpy(dtype=np.float64)


def _depth_sum(df: pd.DataFrame, prefix: str, side: str, bands: tuple[str, ...]) -> np.ndarray:
    n = len(df)
    total = np.zeros(n, dtype=np.float64)
    found = np.zeros(n, dtype=bool)
    for band in bands:
        col = f"{prefix}_{side}_{band}"
        if col not in df.columns:
            continue
        vals = _num(df[col], n)
        ok = np.isfinite(vals)
        total = np.where(ok, total + np.nan_to_num(vals, nan=0.0), total)
        found |= ok
    return np.where(found, total, np.nan)


def _imbalance(bid: np.ndarray, ask: np.ndarray) -> np.ndarray:
    denom = bid + ask
    out = np.full_like(bid, np.nan, dtype=np.float64)
    ok = np.isfinite(bid) & np.isfinite(ask) & (denom > 0)
    out[ok] = (bid[ok] - ask[ok]) / denom[ok]
    return out


def _ratio(part: np.ndarray, whole: np.ndarray) -> np.ndarray:
    out = np.full_like(part, np.nan, dtype=np.float64)
    ok = np.isfinite(part) & np.isfinite(whole) & (whole > 0)
    out[ok] = part[ok] / whole[ok]
    return out


def _asof_value(ts: np.ndarray, values: np.ndarray, query_ts: np.ndarray) -> np.ndarray:
    """Last value at or before each query timestamp (values aligned to ts)."""
    out = np.full(len(query_ts), np.nan, dtype=np.float64)
    if len(ts) == 0:
        return out
    idx = np.searchsorted(ts, query_ts, side="right") - 1
    ok = idx >= 0
    out[ok] = values[idx[ok]]
    return out


def _lagged_return(ts: np.ndarray, px: np.ndarray, lag_ms: int) -> np.ndarray:
    prev = _asof_value(ts, px, ts - lag_ms)
    out = np.full(len(ts), np.nan, dtype=np.float64)
    ok = np.isfinite(px) & np.isfinite(prev) & (prev != 0)
    out[ok] = px[ok] / prev[ok] - 1.0
    return out


def _rolling_std_1s_returns(ts: np.ndarray, px: np.ndarray, window_ms: int) -> np.ndarray:
    """Std of consecutive 1s returns over a trailing window (prefix moments + searchsorted)."""
    n = len(ts)
    out = np.full(n, np.nan, dtype=np.float64)
    r1 = _lagged_return(ts, px, 1000)
    valid = np.isfinite(r1)
    r = np.where(valid, r1, 0.0)
    csum = np.concatenate([[0.0], np.cumsum(r)])
    csum2 = np.concatenate([[0.0], np.cumsum(r * r)])
    ccount = np.concatenate([[0], np.cumsum(valid.astype(np.int64))])
    for i in range(n):
        j0 = int(np.searchsorted(ts, ts[i] - window_ms, side="left"))
        cnt = int(ccount[i + 1] - ccount[j0])
        if cnt < 2:
            continue
        s = csum[i + 1] - csum[j0]
        s2 = csum2[i + 1] - csum2[j0]
        mean = s / cnt
        var = max(0.0, s2 / cnt - mean * mean)
        out[i] = float(np.sqrt(var))
    return out


def _rolling_trade_stats(
    frame_ts: np.ndarray, trades: pd.DataFrame, window_ms: int
) -> dict[str, np.ndarray]:
    n = len(frame_ts)
    out = {
        "trade_count": np.zeros(n, dtype=np.float64),
        "buy_volume": np.zeros(n, dtype=np.float64),
        "sell_volume": np.zeros(n, dtype=np.float64),
        "up_buy_volume": np.zeros(n, dtype=np.float64),
        "down_buy_volume": np.zeros(n, dtype=np.float64),
    }
    if trades is None or trades.empty or "timestamp" not in trades.columns:
        return out

    tdf = trades.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    tts = pd.to_numeric(tdf["timestamp"], errors="coerce").to_numpy(dtype=np.int64)
    shares = (
        pd.to_numeric(tdf["shares"], errors="coerce").fillna(0.0).to_numpy(dtype=np.float64)
        if "shares" in tdf.columns
        else np.ones(len(tdf), dtype=np.float64)
    )
    is_buy = (
        tdf["is_buy"].fillna(False).astype(bool).to_numpy()
        if "is_buy" in tdf.columns
        else np.zeros(len(tdf), dtype=bool)
    )
    is_up = (
        tdf["is_up"].fillna(False).astype(bool).to_numpy()
        if "is_up" in tdf.columns
        else np.zeros(len(tdf), dtype=bool)
    )
    if "is_taker" in tdf.columns:
        is_taker = tdf["is_taker"].fillna(True).astype(bool).to_numpy()
    else:
        is_taker = np.ones(len(tdf), dtype=bool)

    # Keep taker rows only; prefix sums for O(log n) window queries.
    keep = is_taker & np.isfinite(tts)
    tts = tts[keep]
    shares = shares[keep]
    is_buy = is_buy[keep]
    is_up = is_up[keep]
    if len(tts) == 0:
        return out

    ones = np.ones(len(tts), dtype=np.float64)
    buy_sh = np.where(is_buy, shares, 0.0)
    sell_sh = np.where(~is_buy, shares, 0.0)
    up_buy_sh = np.where(is_buy & is_up, shares, 0.0)
    down_buy_sh = np.where(is_buy & ~is_up, shares, 0.0)

    def pref(arr: np.ndarray) -> np.ndarray:
        return np.concatenate([[0.0], np.cumsum(arr)])

    p_count = pref(ones)
    p_buy = pref(buy_sh)
    p_sell = pref(sell_sh)
    p_up_buy = pref(up_buy_sh)
    p_down_buy = pref(down_buy_sh)

    hi = np.searchsorted(tts, frame_ts, side="right")
    lo = np.searchsorted(tts, frame_ts - window_ms, side="left")
    out["trade_count"] = p_count[hi] - p_count[lo]
    out["buy_volume"] = p_buy[hi] - p_buy[lo]
    out["sell_volume"] = p_sell[hi] - p_sell[lo]
    out["up_buy_volume"] = p_up_buy[hi] - p_up_buy[lo]
    out["down_buy_volume"] = p_down_buy[hi] - p_down_buy[lo]
    return out


def engineer_features(
    frame: pd.DataFrame,
    *,
    trades: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return a copy of `frame` with FEATURE_COLUMNS filled from live raw columns."""
    if frame is None or frame.empty or "timestamp" not in frame.columns:
        return frame

    df = frame.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    n = len(df)
    ts = pd.to_numeric(df["timestamp"], errors="coerce").to_numpy(dtype=np.int64)

    btc = _num(df["btc_price"] if "btc_price" in df.columns else None, n)
    if not np.isfinite(btc).any() and "btc_twap_30s" in df.columns:
        btc = _num(df["btc_twap_30s"], n)
    if not np.isfinite(btc).any() and "btc_chainlink" in df.columns:
        btc = _num(df["btc_chainlink"], n)

    open_px = _num(df["btc_open_price"] if "btc_open_price" in df.columns else None, n)
    if np.isfinite(open_px).any():
        # Broadcast first finite open.
        first_open = open_px[np.isfinite(open_px)][0]
        open_s = np.full(n, first_open, dtype=np.float64)
    else:
        first_finite = btc[np.isfinite(btc)]
        open_s = np.full(n, first_finite[0] if len(first_finite) else np.nan, dtype=np.float64)

    up_bid = _num(df["up_bid_price"] if "up_bid_price" in df.columns else None, n)
    up_ask = _num(df["up_ask_price"] if "up_ask_price" in df.columns else None, n)
    down_bid = _num(df["down_bid_price"] if "down_bid_price" in df.columns else None, n)
    down_ask = _num(df["down_ask_price"] if "down_ask_price" in df.columns else None, n)
    up_px = _num(df["up_price"] if "up_price" in df.columns else None, n)
    down_px = _num(df["down_price"] if "down_price" in df.columns else None, n)

    up_mid = (up_bid + up_ask) / 2.0
    down_mid = (down_bid + down_ask) / 2.0
    # Fallback to trade/last when book missing.
    up_mid = np.where(np.isfinite(up_mid), up_mid, up_px)
    down_mid = np.where(np.isfinite(down_mid), down_mid, down_px)

    up_bid_depth = _depth_sum(df, "up", "bid", _DEPTH_BANDS)
    up_ask_depth = _depth_sum(df, "up", "ask", _DEPTH_BANDS)
    down_bid_depth = _depth_sum(df, "down", "bid", _DEPTH_BANDS)
    down_ask_depth = _depth_sum(df, "down", "ask", _DEPTH_BANDS)
    up_near_bid = _depth_sum(df, "up", "bid", _NEAR_BANDS)
    up_near_ask = _depth_sum(df, "up", "ask", _NEAR_BANDS)
    down_near_bid = _depth_sum(df, "down", "bid", _NEAR_BANDS)
    down_near_ask = _depth_sum(df, "down", "ask", _NEAR_BANDS)

    feats: dict[str, Any] = {}
    for lag_s in (1, 5, 10, 30, 60):
        feats[f"btc_return_{lag_s}s"] = _lagged_return(ts, btc, lag_s * 1000)

    feats["btc_momentum_10s"] = feats["btc_return_10s"]
    feats["btc_momentum_30s"] = feats["btc_return_30s"]
    for w in (10, 30, 60):
        feats[f"btc_volatility_{w}s"] = _rolling_std_1s_returns(ts, btc, w * 1000)

    feats["btc_from_open"] = np.where(
        np.isfinite(btc) & np.isfinite(open_s) & (open_s != 0),
        btc / open_s - 1.0,
        np.nan,
    )

    # Trailing 30s high/low via expanding max/min on finite prices (deque would be
    # nicer; for ~300 rows/market a searchsorted slice is fine).
    high30 = np.full(n, np.nan, dtype=np.float64)
    low30 = np.full(n, np.nan, dtype=np.float64)
    j0s = np.searchsorted(ts, ts - 30_000, side="left")
    for i in range(n):
        window = btc[int(j0s[i]) : i + 1]
        window = window[np.isfinite(window)]
        if len(window) == 0:
            continue
        high30[i] = float(np.max(window))
        low30[i] = float(np.min(window))
    feats["btc_high_30s_distance"] = np.where(
        np.isfinite(btc) & np.isfinite(high30) & (btc != 0),
        (high30 - btc) / btc,
        np.nan,
    )
    feats["btc_low_30s_distance"] = np.where(
        np.isfinite(btc) & np.isfinite(low30) & (btc != 0),
        (btc - low30) / btc,
        np.nan,
    )

    feats["up_spread"] = up_ask - up_bid
    feats["down_spread"] = down_ask - down_bid
    feats["up_mid"] = up_mid
    feats["down_mid"] = down_mid
    feats["up_bid_depth"] = up_bid_depth
    feats["up_ask_depth"] = up_ask_depth
    feats["down_bid_depth"] = down_bid_depth
    feats["down_ask_depth"] = down_ask_depth
    feats["up_order_imbalance"] = _imbalance(up_bid_depth, up_ask_depth)
    feats["down_order_imbalance"] = _imbalance(down_bid_depth, down_ask_depth)
    feats["up_near_bid_ratio"] = _ratio(up_near_bid, up_bid_depth)
    feats["up_near_ask_ratio"] = _ratio(up_near_ask, up_ask_depth)
    feats["down_near_bid_ratio"] = _ratio(down_near_bid, down_bid_depth)
    feats["down_near_ask_ratio"] = _ratio(down_near_ask, down_ask_depth)

    for w in (5, 10, 30):
        stats = _rolling_trade_stats(ts, trades if trades is not None else pd.DataFrame(), w * 1000)
        feats[f"trade_count_{w}s"] = stats["trade_count"]
        feats[f"buy_volume_{w}s"] = stats["buy_volume"]
        feats[f"sell_volume_{w}s"] = stats["sell_volume"]
        feats[f"up_buy_volume_{w}s"] = stats["up_buy_volume"]
        feats[f"down_buy_volume_{w}s"] = stats["down_buy_volume"]
        buy = stats["buy_volume"]
        sell = stats["sell_volume"]
        denom = buy + sell
        imb = np.full(n, np.nan, dtype=np.float64)
        ok = denom > 0
        imb[ok] = (buy[ok] - sell[ok]) / denom[ok]
        feats[f"trade_imbalance_{w}s"] = imb

    feats["market_probability_gap"] = np.where(
        np.isfinite(up_px) & np.isfinite(down_px),
        up_px + down_px - 1.0,
        np.nan,
    )
    for lag_s in (5, 10, 30):
        prev_up = _asof_value(ts, up_px, ts - lag_s * 1000)
        chg = np.full(n, np.nan, dtype=np.float64)
        ok = np.isfinite(up_px) & np.isfinite(prev_up) & (prev_up != 0)
        chg[ok] = up_px[ok] / prev_up[ok] - 1.0
        feats[f"up_price_change_{lag_s}s"] = chg

    feats["btc_market_divergence_10s"] = feats["up_price_change_10s"] - feats["btc_return_10s"]
    feats["btc_market_divergence_30s"] = feats["up_price_change_30s"] - feats["btc_return_30s"]

    start = _num(df["start_time"] if "start_time" in df.columns else None, n)
    end = _num(df["end_time"] if "end_time" in df.columns else None, n)
    if not np.isfinite(start).any():
        start = np.full(n, float(ts[0]) if n else np.nan, dtype=np.float64)
    else:
        start = np.full(n, start[np.isfinite(start)][0], dtype=np.float64)
    if not np.isfinite(end).any():
        end = start + 300_000.0
    else:
        end = np.full(n, end[np.isfinite(end)][0], dtype=np.float64)

    elapsed = (ts.astype(np.float64) - start) / 1000.0
    remaining = (end - ts.astype(np.float64)) / 1000.0
    duration = np.maximum((end - start) / 1000.0, 1.0)
    feats["elapsed_seconds"] = elapsed
    feats["remaining_seconds"] = remaining
    feats["market_progress"] = elapsed / duration

    out = df.copy()
    for col in FEATURE_COLUMNS:
        out[col] = feats[col]
    return out


def load_live_feature_frame(market_id: str, *, market_dir: str | Path | None = None) -> pd.DataFrame:
    """Load joined live frame + trades and engineer FEATURE_COLUMNS."""
    mid = str(market_id)
    d = Path(market_dir) if market_dir else find_live_market_dir(mid)
    if d is None:
        raise FileNotFoundError(f"Live market not found: {mid}")
    frame = load_live_market_frame(mid)
    trades = None
    trades_path = d / "trades.parquet"
    if trades_path.is_file():
        try:
            trades = pd.read_parquet(trades_path)
        except Exception:
            trades = None
    return engineer_features(frame, trades=trades)
