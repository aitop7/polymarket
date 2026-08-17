"""Cached inference for the short-horizon Up/Down direction models."""

from __future__ import annotations

import threading
import time
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.data import DIRECTION_FEATURE_COLUMNS
from app.core.live_dataset import find_live_market_dir
from app.ml.live_features import engineer_features, load_live_feature_frame
from app.ml.train_predict_up import direction_model_filename

_MODELS: dict[float, tuple[float, lgb.Booster]] = {}

# Hot-path caches. LightGBM on a single row / small batch is already <<50ms on CPU;
# GPU would add PCIe transfer overhead and is not used for this workload.
_LIVE_HISTORY: dict[str, list[dict[str, Any]]] = {}
_PARQUET_SEED_AT: dict[str, float] = {}
_RESULT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_RESULT_TTL_S = 0.4
_PARQUET_SEED_TTL_S = 30.0
_MAX_HISTORY = 900
_LOCK = threading.Lock()


def _load_model(horizon_seconds: float) -> lgb.Booster:
    path = settings.models_dir / direction_model_filename(horizon_seconds)
    if not path.is_file():
        raise FileNotFoundError(f"Direction model not found: {path.name}")
    stamp = path.stat().st_mtime
    cached = _MODELS.get(float(horizon_seconds))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    model = lgb.Booster(model_file=str(path))
    if model.feature_name() != list(DIRECTION_FEATURE_COLUMNS):
        raise RuntimeError("Direction model feature schema does not match the running application")
    _MODELS[float(horizon_seconds)] = (stamp, model)
    return model


def _finite(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def _band_qty_map(side_rows: list[dict[str, Any]] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in side_rows or []:
        suffix = str(row.get("suffix") or "").strip()
        if not suffix:
            continue
        qty = _finite(row.get("qty"))
        if qty is None:
            continue
        out[suffix] = float(qty)
    return out


def _frame_from_live_snapshot(
    *,
    market_id: str,
    series: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> pd.DataFrame:
    """Build a causal feature frame from the live monitor buffer."""
    start_ms = int(snapshot.get("start_time") or 0) or None
    end_ms = int(snapshot.get("end_time") or 0) or None
    rows: list[dict[str, Any]] = []
    for point in series:
        ts = _finite(point.get("t") if "t" in point else point.get("timestamp"))
        if ts is None:
            continue
        row: dict[str, Any] = {
            "timestamp": int(ts),
            "up_price": _finite(point.get("up") if "up" in point else point.get("up_price")),
            "down_price": _finite(point.get("down") if "down" in point else point.get("down_price")),
            "btc_price": _finite(point.get("btc") if "btc" in point else point.get("btc_price")),
            "btc_chainlink": _finite(
                point.get("chainlink") if "chainlink" in point else point.get("btc_chainlink")
            ),
            "btc_twap_30s": _finite(point.get("twap") if "twap" in point else point.get("btc_twap_30s")),
        }
        if start_ms is not None:
            row["start_time"] = start_ms
        if end_ms is not None:
            row["end_time"] = end_ms
        rows.append(row)

    if not rows:
        now_ms = int(_finite(snapshot.get("timestamp")) or pd.Timestamp.utcnow().timestamp() * 1000)
        rows.append(
            {
                "timestamp": now_ms,
                "up_price": _finite(snapshot.get("up_price")),
                "down_price": _finite(snapshot.get("down_price")),
                "btc_price": _finite(snapshot.get("btc_price")),
                "btc_chainlink": _finite(snapshot.get("btc_chainlink")),
                "btc_twap_30s": _finite(snapshot.get("btc_twap_30s")),
                "start_time": start_ms,
                "end_time": end_ms,
            }
        )

    # Attach the latest CLOB / Binance books onto the newest row so microprice / OBI work.
    last = rows[-1]
    book = snapshot.get("book") if isinstance(snapshot.get("book"), dict) else {}
    for side in ("up", "down"):
        side_book = book.get(side) if isinstance(book, dict) else None
        if not isinstance(side_book, dict):
            continue
        last[f"{side}_bid_price"] = _finite(side_book.get("best_bid"))
        last[f"{side}_ask_price"] = _finite(side_book.get("best_ask"))
        bids = side_book.get("bids") or []
        asks = side_book.get("asks") or []
        bid_shares = _finite(bids[0].get("shares")) if bids else None
        ask_shares = _finite(asks[0].get("shares")) if asks else None
        last[f"{side}_bid_shares"] = bid_shares
        last[f"{side}_ask_shares"] = ask_shares
        last[f"{side}_bid_0_1"] = bid_shares
        last[f"{side}_ask_0_1"] = ask_shares

    binance_book = snapshot.get("binance_book") if isinstance(snapshot.get("binance_book"), dict) else {}
    if isinstance(binance_book, dict):
        ask_map = _band_qty_map(binance_book.get("asks"))
        bid_map = _band_qty_map(binance_book.get("bids"))
        for suffix, qty in ask_map.items():
            last[f"ask_{suffix}"] = qty
        for suffix, qty in bid_map.items():
            last[f"bid_{suffix}"] = qty
        mid_px = _finite(binance_book.get("mid") or binance_book.get("price") or snapshot.get("btc_price"))
        if mid_px is not None:
            last["btc_price"] = mid_px

    # Prefer wall-clock / snapshot time for the tip so age_ms stays near zero.
    snap_ts = _finite(snapshot.get("timestamp"))
    if snap_ts is not None and int(snap_ts) >= int(last["timestamp"]):
        last["timestamp"] = int(snap_ts)

    # Tip scoring only needs a short causal lookback (returns / OBI change).
    if len(rows) > 120:
        rows = rows[-120:]

    frame = pd.DataFrame(rows).sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    frame.attrs["market_id"] = str(market_id)
    return engineer_features(frame)


def _row_values(row: pd.Series) -> tuple[np.ndarray, float]:
    values = pd.to_numeric(row.reindex(DIRECTION_FEATURE_COLUMNS), errors="coerce").to_numpy(
        dtype=np.float32
    )
    coverage = float(np.mean(np.isfinite(values)))
    return values, coverage


def _score_values(values: np.ndarray, *, horizons: tuple[float, ...]) -> list[dict[str, Any]]:
    predictions: list[dict[str, Any]] = []
    for horizon in horizons:
        probability_up = float(_load_model(float(horizon)).predict(values.reshape(1, -1))[0])
        confidence = abs(probability_up - 0.5) * 2.0
        predictions.append(
            {
                "horizon_seconds": float(horizon),
                "probability_up": probability_up,
                "probability_down": 1.0 - probability_up,
                "direction": "UP" if probability_up >= 0.5 else "DOWN",
                "confidence": confidence,
            }
        )
    return predictions


def _score_feature_row(
    row: pd.Series, *, horizons: tuple[float, ...], min_coverage: float = 0.35
) -> dict[str, Any]:
    values, feature_coverage = _row_values(row)
    if feature_coverage < min_coverage:
        raise RuntimeError(
            f"Insufficient live feature coverage ({feature_coverage:.0%}); wait for market data to accumulate"
        )

    timestamp = int(pd.to_numeric(row.get("timestamp"), errors="coerce"))
    now_ms = int(pd.Timestamp.utcnow().timestamp() * 1000)
    return {
        "timestamp": timestamp,
        "age_ms": max(0, now_ms - timestamp),
        "feature_coverage": feature_coverage,
        "predictions": _score_values(values, horizons=horizons),
    }


def _score_history(
    df: pd.DataFrame, *, horizons: tuple[float, ...], max_points: int
) -> list[dict[str, Any]]:
    """Batch-score recent rows (used to seed the live chart once from parquet)."""
    tail = df.tail(max(1, int(max_points)))
    features = tail.reindex(columns=DIRECTION_FEATURE_COLUMNS).apply(pd.to_numeric, errors="coerce")
    matrix = features.to_numpy(dtype=np.float32)
    if not len(matrix):
        return []

    timestamps = pd.to_numeric(tail.get("timestamp"), errors="coerce").to_numpy(dtype="float64")
    coverage = np.mean(np.isfinite(matrix), axis=1)
    scored_by_horizon = {float(h): _load_model(float(h)).predict(matrix) for h in horizons}

    out: list[dict[str, Any]] = []
    for i, ts in enumerate(timestamps):
        if not np.isfinite(ts) or coverage[i] < 0.35:
            continue
        point: dict[str, Any] = {"timestamp": int(ts)}
        for horizon, probabilities in scored_by_horizon.items():
            tag = f"{horizon:g}".replace(".", "p")
            point[f"p_up_{tag}s"] = float(probabilities[i])
        out.append(point)
    return out


def _history_point(scored: dict[str, Any]) -> dict[str, Any]:
    point: dict[str, Any] = {"timestamp": int(scored["timestamp"])}
    for pred in scored.get("predictions") or []:
        horizon = float(pred["horizon_seconds"])
        tag = f"{horizon:g}".replace(".", "p")
        point[f"p_up_{tag}s"] = float(pred["probability_up"])
    return point


def _merge_history(
    market_id: str, scored: dict[str, Any], *, seed: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    point = _history_point(scored)
    with _LOCK:
        prev = list(_LIVE_HISTORY.get(market_id) or [])
        if seed and not prev:
            prev = list(seed)
        merged = [p for p in prev if abs(int(p["timestamp"]) - point["timestamp"]) > 120]
        merged.append(point)
        merged.sort(key=lambda p: int(p["timestamp"]))
        if len(merged) > _MAX_HISTORY:
            merged = merged[-_MAX_HISTORY:]
        _LIVE_HISTORY[market_id] = merged
        return list(merged)


def _maybe_seed_parquet(market_id: str, *, horizons: tuple[float, ...]) -> list[dict[str, Any]]:
    now = time.monotonic()
    with _LOCK:
        last = _PARQUET_SEED_AT.get(market_id, 0.0)
        if _LIVE_HISTORY.get(market_id) and (now - last) < _PARQUET_SEED_TTL_S:
            return []
        _PARQUET_SEED_AT[market_id] = now

    try:
        if find_live_market_dir(market_id) is None:
            return []
        df = load_live_feature_frame(market_id)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    return _score_history(df, horizons=horizons, max_points=240)


def get_cached_prediction(market_id: str) -> dict[str, Any] | None:
    """Return a still-fresh cached score without recomputing."""
    mid = str(market_id)
    now_mono = time.monotonic()
    with _LOCK:
        cached = _RESULT_CACHE.get(mid)
        if cached is not None and (now_mono - cached[0]) < _RESULT_TTL_S:
            return dict(cached[1])
    return None


def predict_direction(
    market_id: str,
    *,
    horizons: tuple[float, ...] = (3.0, 5.0),
    series: list[dict[str, Any]] | None = None,
    snapshot: dict[str, Any] | None = None,
    history_points: int = 300,
    prefer_live: bool = True,
) -> dict[str, Any]:
    """Score the latest live tip and maintain a rolling probability curve.

    Prefers the in-memory live monitor (realtime clock). Falls back to parquet
    only when live feature coverage is insufficient. Chart history is a rolling
    append of live scores (optionally seeded once from parquet).
    """
    del history_points  # retained for API compatibility; rolling cache owns length
    mid = str(market_id)
    now_mono = time.monotonic()
    with _LOCK:
        cached_result = _RESULT_CACHE.get(mid)
        if cached_result is not None and (now_mono - cached_result[0]) < _RESULT_TTL_S:
            return dict(cached_result[1])

    source = "live_buffer"
    scored: dict[str, Any] | None = None
    live_error: Exception | None = None

    if prefer_live and snapshot is not None:
        try:
            live_df = _frame_from_live_snapshot(
                market_id=mid,
                series=series or [],
                snapshot=snapshot,
            )
            if not live_df.empty:
                scored = _score_feature_row(live_df.iloc[-1], horizons=horizons, min_coverage=0.30)
                source = "live_buffer"
                # Stamp tip to wall clock so chips/chart track realtime, not REST snapshot lag.
                now_ms = int(time.time() * 1000)
                scored["timestamp"] = now_ms
                scored["age_ms"] = 0
        except Exception as exc:  # coverage / empty — try parquet
            live_error = exc
            scored = None

    if scored is None:
        try:
            if find_live_market_dir(mid) is None:
                raise FileNotFoundError(mid)
            df = load_live_feature_frame(mid)
            if df.empty:
                raise RuntimeError("No live data for this market yet")
            scored = _score_feature_row(df.iloc[-1], horizons=horizons)
            source = "parquet"
            # Align age to live series tip when available so the UI doesn't look frozen.
            if series:
                tip = _finite((series[-1] or {}).get("t"))
                if tip is not None:
                    now_ms = int(pd.Timestamp.utcnow().timestamp() * 1000)
                    scored["timestamp"] = int(tip)
                    scored["age_ms"] = max(0, now_ms - int(tip))
        except FileNotFoundError:
            if live_error is not None:
                raise live_error from None
            if not snapshot:
                raise FileNotFoundError(
                    f"Live market not found locally yet: {mid}. Waiting for capture/sync."
                ) from None
            raise
        except Exception:
            if live_error is not None:
                raise live_error from None
            raise

    seed = _maybe_seed_parquet(mid, horizons=horizons) if source == "live_buffer" else None
    history = _merge_history(mid, scored, seed=seed)

    result = {
        "market_id": mid,
        "source": source,
        "history": history,
        **scored,
    }
    with _LOCK:
        _RESULT_CACHE[mid] = (time.monotonic(), result)
    return result
