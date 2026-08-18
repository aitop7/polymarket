"""Cached inference for the short-horizon Up/Down direction models."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.data import DIRECTION_FEATURE_COLUMNS, FEATURE_COLUMNS
from app.core.live_dataset import find_live_market_dir
from app.ml.live_features import engineer_features, load_live_feature_frame
from app.ml.train_predict_up import (
    direction_model_filename,
    metrics_filename,
    model_filename,
)

_MODELS: dict[float, tuple[float, lgb.Booster]] = {}
_LEVEL_MODELS: dict[float, tuple[float, lgb.Booster]] = {}
_LEVEL_STD: dict[float, float] = {}

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


def _load_level_model(horizon_seconds: float) -> lgb.Booster:
    path = settings.models_dir / model_filename(horizon_seconds)
    if not path.is_file():
        raise FileNotFoundError(f"Level model not found: {path.name}")
    stamp = path.stat().st_mtime
    cached = _LEVEL_MODELS.get(float(horizon_seconds))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    model = lgb.Booster(model_file=str(path))
    _LEVEL_MODELS[float(horizon_seconds)] = (stamp, model)
    return model


def _level_residual_std(horizon_seconds: float) -> float:
    """Use held-out RMSE as the predictive σ for a Normal density around the mean."""
    h = float(horizon_seconds)
    cached = _LEVEL_STD.get(h)
    if cached is not None:
        return cached
    path = settings.models_dir / metrics_filename(h)
    std = 0.04
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rmse = payload.get("test", {}).get("rmse")
        if rmse is not None and float(rmse) > 0:
            std = float(rmse)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    _LEVEL_STD[h] = std
    return std


def _normal_pdf(xs: np.ndarray, mean: float, std: float) -> np.ndarray:
    if std <= 1e-9:
        out = np.zeros_like(xs, dtype=np.float64)
        idx = int(np.argmin(np.abs(xs - mean)))
        out[idx] = 1.0
        return out
    z = (xs - mean) / std
    return (1.0 / (std * np.sqrt(2.0 * np.pi))) * np.exp(-0.5 * z * z)


def _score_beta_row(row: pd.Series, *, horizons: tuple[float, ...]) -> dict[str, Any]:
    """Score Beta density heads for each requested horizon t (exact or interpolated)."""
    from app.ml.price_distribution import future_up_price_pdf

    values = pd.to_numeric(row.reindex(FEATURE_COLUMNS), errors="coerce").to_numpy(dtype=np.float32)
    feature_coverage = float(np.mean(np.isfinite(values)))
    if feature_coverage < 0.25:
        raise RuntimeError(
            f"Insufficient live feature coverage ({feature_coverage:.0%}); wait for market data to accumulate"
        )
    current = _finite(row.get("up_mid"))
    if current is None:
        current = _finite(row.get("up_price"))
    if current is None:
        current = 0.5

    predictions: list[dict[str, Any]] = []
    for horizon in horizons:
        dist = future_up_price_pdf(
            float(horizon),
            values,
            family="beta",
            current_up=float(current),
        )
        predictions.append(
            {
                "horizon_seconds": float(horizon),
                "probability_up": dist["probability_up"],
                "probability_down": dist["probability_down"],
                "direction": dist["direction"],
                "confidence": dist["confidence"],
                "mean": dist["mean"],
                "variance": dist["variance"],
                "std": dist["std"],
                "alpha": dist["alpha"],
                "beta": dist["beta"],
                "source": dist.get("source"),
            }
        )

    timestamp = int(pd.to_numeric(row.get("timestamp"), errors="coerce"))
    now_ms = int(pd.Timestamp.utcnow().timestamp() * 1000)
    return {
        "timestamp": timestamp,
        "age_ms": max(0, now_ms - timestamp),
        "feature_coverage": feature_coverage,
        "predictions": predictions,
        "model_kind": "beta",
    }


def _predict_distribution_for_horizon(
    row: pd.Series,
    *,
    horizon: float,
    direction_predictions: list[dict[str, Any]] | None = None,
    family: str = "level",
) -> dict[str, Any] | None:
    """Build a predictive Up-price density for one horizon t via future_up_price_pdf."""
    from app.ml.price_distribution import future_up_price_pdf

    current = _finite(row.get("up_mid"))
    if current is None:
        current = _finite(row.get("up_price"))
    if current is None:
        current = 0.5

    pdf_family = "beta" if family == "beta" else "level"
    try:
        values = pd.to_numeric(row.reindex(FEATURE_COLUMNS), errors="coerce").to_numpy(dtype=np.float32)
        dist = future_up_price_pdf(
            float(horizon),
            values,
            family=pdf_family,  # type: ignore[arg-type]
            current_up=float(current),
        )
        return {
            "horizon_seconds": float(horizon),
            "mean": dist["mean"],
            "variance": dist["variance"],
            "std": dist["std"],
            "alpha": dist.get("alpha"),
            "beta": dist.get("beta"),
            "current_up": float(current),
            "source": dist.get("source"),
            "family": dist.get("family"),
            "pdf": dist["pdf"],
        }
    except FileNotFoundError:
        # Fall back to direction-approx Normal when no density artifacts exist.
        if pdf_family == "beta":
            return None
        p_up = 0.5
        for pred in direction_predictions or []:
            if abs(float(pred.get("horizon_seconds", -1)) - float(horizon)) < 1e-9:
                p_up = float(pred.get("probability_up", 0.5))
                break
        expected_move = 0.02 * abs(2.0 * p_up - 1.0)
        sign = 1.0 if p_up >= 0.5 else -1.0
        mean = float(np.clip(current + sign * expected_move, 0.0, 1.0))
        std = 0.04
        xs = np.linspace(1e-4, 1.0 - 1e-4, 161, dtype=np.float64)
        dens = _normal_pdf(xs, mean, std)
        trapz = getattr(np, "trapezoid", None) or np.trapz
        area = float(trapz(dens, xs)) if len(xs) > 1 else 1.0
        if area > 0:
            dens = dens / area
        return {
            "horizon_seconds": float(horizon),
            "mean": mean,
            "variance": float(std * std),
            "std": float(std),
            "current_up": float(current),
            "source": "direction_approx",
            "family": "normal",
            "pdf": [{"x": float(x), "density": float(y)} for x, y in zip(xs, dens)],
        }


def _predict_distribution(
    row: pd.Series,
    *,
    horizons: tuple[float, ...],
    direction_predictions: list[dict[str, Any]] | None = None,
    family: str = "level",
) -> dict[str, Any] | None:
    """Primary (shortest) horizon density — kept for backward-compatible clients."""
    ordered = tuple(sorted({float(h) for h in horizons})) or (3.0,)
    return _predict_distribution_for_horizon(
        row,
        horizon=ordered[0],
        direction_predictions=direction_predictions,
        family=family,
    )


def _predict_distributions(
    row: pd.Series,
    *,
    horizons: tuple[float, ...],
    direction_predictions: list[dict[str, Any]] | None = None,
    family: str = "level",
) -> list[dict[str, Any]]:
    """Densities for every active horizon (e.g. 3s and 5s overlays)."""
    ordered = tuple(sorted({float(h) for h in horizons})) or (3.0,)
    out: list[dict[str, Any]] = []
    for horizon in ordered:
        dist = _predict_distribution_for_horizon(
            row,
            horizon=horizon,
            direction_predictions=direction_predictions,
            family=family,
        )
        if dist is not None:
            out.append(dist)
    return out


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
        if pred.get("mean") is not None:
            try:
                point[f"mean_{tag}s"] = float(pred["mean"])
            except (TypeError, ValueError):
                pass
        if pred.get("std") is not None:
            try:
                point[f"std_{tag}s"] = float(pred["std"])
            except (TypeError, ValueError):
                pass
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


def get_cached_prediction(
    market_id: str,
    *,
    horizons: tuple[float, ...] | None = None,
    model_kind: str | None = None,
) -> dict[str, Any] | None:
    """Return a still-fresh cached score without recomputing."""
    mid = str(market_id)
    now_mono = time.monotonic()
    with _LOCK:
        cached = _RESULT_CACHE.get(mid)
        if cached is not None and (now_mono - cached[0]) < _RESULT_TTL_S:
            result = cached[1]
            cached_horizons = tuple(
                float(p["horizon_seconds"]) for p in result.get("predictions") or []
            )
            cached_kind = str(result.get("model_kind") or "direction")
            if model_kind is not None and cached_kind != model_kind:
                return None
            if horizons is None or cached_horizons == tuple(float(h) for h in horizons):
                return dict(result)
    return None


def clear_prediction_cache() -> None:
    """Drop live prediction caches after the active model family changes."""
    with _LOCK:
        _RESULT_CACHE.clear()
        _LIVE_HISTORY.clear()
        _PARQUET_SEED_AT.clear()


def predict_direction(
    market_id: str,
    *,
    horizons: tuple[float, ...] = (3.0, 5.0),
    series: list[dict[str, Any]] | None = None,
    snapshot: dict[str, Any] | None = None,
    history_points: int = 300,
    prefer_live: bool = True,
    model_kind: str = "direction",
) -> dict[str, Any]:
    """Score the latest live tip for the active model family.

    Prefers the in-memory live monitor (realtime clock). Falls back to parquet
    only when live feature coverage is insufficient. Chart history is a rolling
    append of live scores (optionally seeded once from parquet).
    """
    del history_points  # retained for API compatibility; rolling cache owns length
    kind = "beta" if model_kind == "beta" else "direction"
    mid = str(market_id)
    now_mono = time.monotonic()
    with _LOCK:
        cached_result = _RESULT_CACHE.get(mid)
        if cached_result is not None and (now_mono - cached_result[0]) < _RESULT_TTL_S:
            cached = cached_result[1]
            cached_kind = str(cached.get("model_kind") or "direction")
            cached_horizons = tuple(
                float(p["horizon_seconds"]) for p in cached.get("predictions") or []
            )
            if cached_kind == kind and cached_horizons == tuple(float(h) for h in horizons):
                return dict(cached)

    source = "live_buffer"
    scored: dict[str, Any] | None = None
    feature_row: pd.Series | None = None
    live_error: Exception | None = None

    def _score_row(row: pd.Series, *, min_coverage: float) -> dict[str, Any]:
        if kind == "beta":
            return _score_beta_row(row, horizons=horizons)
        out = _score_feature_row(row, horizons=horizons, min_coverage=min_coverage)
        out["model_kind"] = "direction"
        return out

    if prefer_live and snapshot is not None:
        try:
            live_df = _frame_from_live_snapshot(
                market_id=mid,
                series=series or [],
                snapshot=snapshot,
            )
            if not live_df.empty:
                feature_row = live_df.iloc[-1]
                scored = _score_row(feature_row, min_coverage=0.30)
                source = "live_buffer"
                now_ms = int(time.time() * 1000)
                scored["timestamp"] = now_ms
                scored["age_ms"] = 0
        except Exception as exc:  # coverage / empty — try parquet
            live_error = exc
            scored = None
            feature_row = None

    if scored is None:
        try:
            if find_live_market_dir(mid) is None:
                raise FileNotFoundError(mid)
            df = load_live_feature_frame(mid)
            if df.empty:
                raise RuntimeError("No live data for this market yet")
            feature_row = df.iloc[-1]
            scored = _score_row(feature_row, min_coverage=0.35)
            source = "parquet"
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

    seed = (
        _maybe_seed_parquet(mid, horizons=horizons)
        if source == "live_buffer" and kind == "direction"
        else None
    )
    history = _merge_history(mid, scored, seed=seed)
    distributions = (
        _predict_distributions(
            feature_row,
            horizons=horizons,
            direction_predictions=list(scored.get("predictions") or []),
            family="beta" if kind == "beta" else "level",
        )
        if feature_row is not None
        else []
    )
    distribution = distributions[0] if distributions else None

    result = {
        "market_id": mid,
        "source": source,
        "model_kind": kind,
        "history": history,
        "distribution": distribution,
        "distributions": distributions,
        **scored,
    }
    with _LOCK:
        _RESULT_CACHE[mid] = (time.monotonic(), result)
    return result
