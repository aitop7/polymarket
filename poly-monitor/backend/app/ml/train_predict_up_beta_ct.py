"""Continuous-time Beta density: μ(X,t), σ²(X,t) for any t > 0.

Unlike per-horizon Beta heads, this trains a single pair of LightGBM models that
take the forecast horizon as input features:

    [FEATURE_COLUMNS…, t, log(t), sqrt(t)]

so ``future_up_price_pdf(t, X)`` is a true function of continuous time.

Artifacts (never overwrite discrete h* Beta models):
  predict_up_beta_ct_mean.txt
  predict_up_beta_ct_logvar.txt
  predict_up_beta_ct_metrics.json
  predict_up_beta_ct_eval.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Sequence

import lightgbm as lgb
import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.data import FEATURE_COLUMNS
from app.ml.live_features import load_live_feature_frame
from app.ml.train_predict_up import (
    DEFAULT_PARAMS,
    _emit,
    _inner_val_split,
    chronological_split,
    list_live_markets,
)
from app.ml.train_predict_up_beta import _clip_mean, _metrics

TIME_FEATURE_NAMES = ("horizon_t", "horizon_log_t", "horizon_sqrt_t")
CT_FEATURE_COLUMNS: tuple[str, ...] = tuple(FEATURE_COLUMNS) + TIME_FEATURE_NAMES

CT_MEAN_FILENAME = "predict_up_beta_ct_mean.txt"
CT_LOGVAR_FILENAME = "predict_up_beta_ct_logvar.txt"
CT_METRICS_FILENAME = "predict_up_beta_ct_metrics.json"
CT_EVAL_FILENAME = "predict_up_beta_ct_eval.json"

# Parallel parquet/feature load. Threads are safer than processes when training
# runs inside a FastAPI daemon worker thread on Windows.
_DEFAULT_LOAD_WORKERS = min(32, max(8, (os.cpu_count() or 4) * 2))


def time_features(t_seconds: float | np.ndarray) -> np.ndarray:
    """Map continuous horizon t → (t, log t, √t)."""
    t = np.asarray(t_seconds, dtype=np.float64)
    scalar = t.ndim == 0
    t = np.atleast_1d(t)
    t = np.clip(t, 1e-3, 1e3)
    out = np.column_stack([t, np.log(t), np.sqrt(t)]).astype(np.float32)
    return out[0] if scalar else out


def augment_features(X: np.ndarray, t_seconds: float | np.ndarray) -> np.ndarray:
    """Append continuous-time features to a market feature matrix / row."""
    base = np.asarray(X, dtype=np.float32)
    tf = time_features(t_seconds)
    if base.ndim == 1:
        return np.concatenate([base, np.atleast_1d(tf).astype(np.float32)])
    if tf.ndim == 1:
        tf = np.repeat(tf.reshape(1, -1), base.shape[0], axis=0)
    return np.concatenate([base, tf.astype(np.float32)], axis=1)


def continuous_model_paths() -> dict[str, Any]:
    return {
        "mean": settings.models_dir / CT_MEAN_FILENAME,
        "logvar": settings.models_dir / CT_LOGVAR_FILENAME,
        "metrics": settings.models_dir / CT_METRICS_FILENAME,
        "eval": settings.models_dir / CT_EVAL_FILENAME,
    }


def continuous_model_ready() -> bool:
    paths = continuous_model_paths()
    return paths["mean"].is_file() and paths["logvar"].is_file()


def _train_regressor(X: np.ndarray, y: np.ndarray, *, rounds: int, seed: int) -> lgb.Booster:
    params = {
        **DEFAULT_PARAMS,
        "metric": "None",
        "seed": seed,
        "min_child_samples": 80,
        "num_leaves": 48,
    }
    return lgb.train(
        params,
        lgb.Dataset(X, label=y, feature_name=list(CT_FEATURE_COLUMNS)),
        num_boost_round=max(40, int(rounds)),
    )


def _sample_horizons(
    n: int,
    *,
    t_min: float,
    t_max: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Log-uniform continuous horizons in [t_min, t_max]."""
    lo = math.log(max(1e-3, float(t_min)))
    hi = math.log(max(float(t_min) + 1e-6, float(t_max)))
    return np.exp(rng.uniform(lo, hi, size=int(n))).astype(np.float64)


def _load_market_arrays(
    market: dict[str, Any],
    *,
    feature_columns: Sequence[str] = FEATURE_COLUMNS,
) -> tuple[str, np.ndarray, np.ndarray, np.ndarray, np.ndarray, int | None] | None:
    """Load one market once → (id, X_keep, keep_idx, ts_full, prices_full, end_ms)."""
    mid = str(market["market_id"])
    try:
        df = load_live_feature_frame(mid, market_dir=market.get("dir"))
    except Exception:
        return None
    if df.empty or "timestamp" not in df.columns:
        return None
    if not df["timestamp"].is_monotonic_increasing:
        df = df.sort_values("timestamp", kind="mergesort")
    df = df.reset_index(drop=True)

    start_ms = market.get("start_time")
    end_ms = market.get("end_time")
    if "start_time" in df.columns and pd.notna(df["start_time"].iloc[0]):
        try:
            start_ms = int(df["start_time"].iloc[0])
        except (TypeError, ValueError):
            pass
    if "end_time" in df.columns and pd.notna(df["end_time"].iloc[0]):
        try:
            end_ms = int(df["end_time"].iloc[0])
        except (TypeError, ValueError):
            pass

    if "up_mid" in df.columns:
        prices = pd.to_numeric(df["up_mid"], errors="coerce").to_numpy(dtype=np.float64)
    elif "up_price" in df.columns:
        prices = pd.to_numeric(df["up_price"], errors="coerce").to_numpy(dtype=np.float64)
    else:
        return None

    ts = pd.to_numeric(df["timestamp"], errors="coerce").to_numpy(dtype=np.int64)
    n = len(df)
    cols: list[np.ndarray] = []
    for col in feature_columns:
        if col in df.columns:
            cols.append(pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=np.float32))
        else:
            cols.append(np.full(n, np.nan, dtype=np.float32))
    X = np.column_stack(cols)

    in_window = np.ones(n, dtype=bool)
    if start_ms is not None:
        in_window &= ts >= int(start_ms)
    if end_ms is not None:
        in_window &= ts < int(end_ms)
    cov = np.mean(np.isfinite(X), axis=1)
    keep = in_window & np.isfinite(prices) & (cov >= 0.25)
    keep_idx = np.flatnonzero(keep)
    if keep_idx.size == 0:
        return None
    return mid, X[keep_idx], keep_idx.astype(np.int64), ts, prices, (int(end_ms) if end_ms is not None else None)


def _sample_from_arrays(
    X_keep: np.ndarray,
    keep_idx: np.ndarray,
    ts_full: np.ndarray,
    prices_full: np.ndarray,
    end_ms: int | None,
    *,
    t_min: float,
    t_max: float,
    samples_per_row: int,
    row_stride: int,
    seed: int,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Vectorized continuous-t labels; future price looked up on the full timeline."""
    if X_keep.size == 0 or keep_idx.size == 0:
        return None
    local = np.arange(len(keep_idx), dtype=np.int64)
    if row_stride > 1:
        local = local[:: max(1, int(row_stride))]
    if local.size == 0:
        return None

    rng = np.random.default_rng(seed)
    reps = max(1, int(samples_per_row))
    local_rep = np.repeat(local, reps)
    full_rep = keep_idx[local_rep]
    t = _sample_horizons(len(local_rep), t_min=t_min, t_max=t_max, rng=rng)
    target_ts = ts_full[full_rep] + np.rint(t * 1000.0).astype(np.int64)
    j = np.searchsorted(ts_full, target_ts, side="left")
    valid = j < len(ts_full)
    if end_ms is not None:
        valid &= target_ts <= int(end_ms)
    j_safe = np.clip(j, 0, len(prices_full) - 1)
    valid &= np.isfinite(prices_full[j_safe])
    if not np.any(valid):
        return None

    X_aug = augment_features(X_keep[local_rep[valid]], t[valid])
    y = prices_full[j[valid]].astype(np.float32)
    return X_aug, y


def _preload_markets_parallel(
    markets: list[dict[str, Any]],
    *,
    workers: int,
    progress_cb: Any = None,
    phase_label: str = "load",
    progress_lo: float = 0,
    progress_hi: float = 40,
) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int | None]]:
    """Load+engineer all markets concurrently (one pass)."""
    out: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int | None]] = {}
    n = max(1, len(markets))
    if not markets:
        return out

    workers = max(1, min(int(workers), len(markets)))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_load_market_arrays, m) for m in markets]
        for fut in as_completed(futs):
            done += 1
            try:
                packed = fut.result()
            except Exception:
                packed = None
            if packed is not None:
                mid, X_keep, keep_idx, ts, prices, end_ms = packed
                out[mid] = (X_keep, keep_idx, ts, prices, end_ms)
            if progress_cb is not None and (done % 20 == 0 or done == n):
                frac = done / n
                _emit(
                    progress_cb,
                    phase=phase_label,
                    progress=progress_lo + (progress_hi - progress_lo) * frac,
                    message=f"Loaded {done}/{n} markets ({len(out)} usable, {workers} workers)",
                )
    return out


def _stack_from_cache(
    markets: list[dict[str, Any]],
    cache: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int | None]],
    *,
    t_min: float,
    t_max: float,
    samples_per_row: int,
    row_stride: int,
    seed: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Sample (X,t) rows from preloaded market arrays (no disk I/O)."""
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    for i, market in enumerate(markets):
        packed = cache.get(str(market["market_id"]))
        if packed is None:
            continue
        X_keep, keep_idx, ts, prices, end_ms = packed
        sampled = _sample_from_arrays(
            X_keep,
            keep_idx,
            ts,
            prices,
            end_ms,
            t_min=t_min,
            t_max=t_max,
            samples_per_row=samples_per_row,
            row_stride=row_stride,
            seed=seed + i * 9973,
        )
        if sampled is None:
            continue
        x, y = sampled
        xs.append(x)
        ys.append(y)
    if not xs:
        return np.zeros((0, len(CT_FEATURE_COLUMNS)), dtype=np.float32), np.zeros(0, dtype=np.float32)
    return np.vstack(xs), np.concatenate(ys)


def _stack_continuous(
    markets: list[dict[str, Any]],
    *,
    t_min: float,
    t_max: float,
    samples_per_row: int,
    row_stride: int,
    seed: int,
    progress_cb: Any = None,
    phase_label: str = "load",
    progress_lo: float = 0,
    progress_hi: float = 40,
    workers: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Backward-compatible stack: parallel preload then sample."""
    cache = _preload_markets_parallel(
        markets,
        workers=workers or _DEFAULT_LOAD_WORKERS,
        progress_cb=progress_cb,
        phase_label=phase_label,
        progress_lo=progress_lo,
        progress_hi=progress_hi,
    )
    return _stack_from_cache(
        markets,
        cache,
        t_min=t_min,
        t_max=t_max,
        samples_per_row=samples_per_row,
        row_stride=row_stride,
        seed=seed,
    )


def train_beta_continuous(
    *,
    t_min: float = 0.5,
    t_max: float = 30.0,
    samples_per_row: int = 1,
    row_stride: int = 4,
    train_ratio: float = 0.9,
    num_boost_round: int = 300,
    max_markets: int | None = None,
    load_workers: int | None = None,
    progress_cb: Any = None,
) -> dict[str, Any]:
    """Train continuous-time Beta μ(X,t) and log-variance heads.

    Markets are loaded **once in parallel**, then fit/validation/test samples are
    drawn from the in-memory cache (no repeated parquet/feature engineering).
    """
    markets = list_live_markets(max_markets=max_markets)
    if len(markets) < 8:
        raise RuntimeError("Need at least eight closed markets for continuous-t Beta")
    train_markets, test_markets = chronological_split(markets, train_ratio=train_ratio)
    fit_markets, variance_markets = _inner_val_split(train_markets, val_frac=0.2)
    workers = load_workers or _DEFAULT_LOAD_WORKERS

    _emit(
        progress_cb,
        phase="load",
        progress=2,
        message=f"Continuous-t Beta: parallel load {len(markets)} markets ({workers} workers)",
    )
    # Single disk pass over every market used by any split.
    cache = _preload_markets_parallel(
        markets,
        workers=workers,
        progress_cb=progress_cb,
        phase_label="load",
        progress_lo=2,
        progress_hi=48,
    )
    if len(cache) < 8:
        raise RuntimeError(f"Only {len(cache)} markets loaded usable features")

    _emit(progress_cb, phase="sample", progress=50, message="Sampling continuous-t labels from cache")
    X_fit, y_fit = _stack_from_cache(
        fit_markets,
        cache,
        t_min=t_min,
        t_max=t_max,
        samples_per_row=samples_per_row,
        row_stride=row_stride,
        seed=11,
    )
    X_var, y_var = _stack_from_cache(
        variance_markets,
        cache,
        t_min=t_min,
        t_max=t_max,
        samples_per_row=samples_per_row,
        row_stride=row_stride,
        seed=13,
    )
    X_test, y_test = _stack_from_cache(
        test_markets,
        cache,
        t_min=t_min,
        t_max=t_max,
        samples_per_row=max(1, samples_per_row),
        row_stride=row_stride,
        seed=17,
    )
    # Free feature matrices before boosting.
    cache.clear()

    if min(len(y_fit), len(y_var), len(y_test)) < 200:
        raise RuntimeError("Too few continuous-t samples in train/validation/test split")

    _emit(progress_cb, phase="train", progress=55, message="Training continuous-t mean head μ(X,t)")
    mean_model = _train_regressor(X_fit, y_fit, rounds=num_boost_round, seed=71)
    mu_var = _clip_mean(mean_model.predict(X_var))
    log_residual_var = np.log(np.maximum((y_var - mu_var) ** 2, 1e-6))

    _emit(progress_cb, phase="train", progress=75, message="Training continuous-t variance head")
    variance_model = _train_regressor(
        X_var, log_residual_var, rounds=max(100, num_boost_round // 2), seed=73
    )

    def predict(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        mu = _clip_mean(mean_model.predict(X))
        var = np.exp(np.clip(variance_model.predict(X), math.log(1e-6), math.log(0.25)))
        return mu, np.minimum(var, mu * (1.0 - mu) * 0.995)

    mu_fit, var_fit = predict(X_fit)
    mu_v, var_v = predict(X_var)
    mu_te, var_te = predict(X_test)
    metrics: dict[str, Any] = {
        "task": "predict_up_beta_continuous_t",
        "distribution": "Beta(alpha=mu*kappa, beta=(1-mu)*kappa)",
        "time_model": "continuous",
        "t_min": float(t_min),
        "t_max": float(t_max),
        "samples_per_row": int(samples_per_row),
        "row_stride": int(row_stride),
        "load_workers": int(workers),
        "feature_columns": list(CT_FEATURE_COLUMNS),
        "n_features": len(CT_FEATURE_COLUMNS),
        "n_markets": {"fit": len(fit_markets), "variance": len(variance_markets), "test": len(test_markets)},
        "n_rows": {"fit": int(len(y_fit)), "variance": int(len(y_var)), "test": int(len(y_test))},
        "train": _metrics(y_fit, mu_fit, var_fit),
        "validation": _metrics(y_var, mu_v, var_v),
        "test": _metrics(y_test, mu_te, var_te),
    }
    paths = continuous_model_paths()
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    mean_model.save_model(str(paths["mean"]))
    variance_model.save_model(str(paths["logvar"]))
    paths["metrics"].write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    _emit(progress_cb, phase="save", progress=100, message="Saved continuous-t Beta μ(X,t)", metrics=metrics)
    return {
        "mean_model_path": str(paths["mean"]),
        "variance_model_path": str(paths["logvar"]),
        "metrics": metrics,
    }


def evaluate_beta_continuous(
    *,
    t_min: float = 0.5,
    t_max: float = 30.0,
    samples_per_row: int = 1,
    row_stride: int = 4,
    train_ratio: float = 0.9,
    max_markets: int | None = None,
    load_workers: int | None = None,
) -> dict[str, Any]:
    paths = continuous_model_paths()
    if not continuous_model_ready():
        raise FileNotFoundError("Continuous-t Beta models not found — train first")
    _train, test_markets = chronological_split(list_live_markets(max_markets=max_markets), train_ratio=train_ratio)
    X, y = _stack_continuous(
        test_markets,
        t_min=t_min,
        t_max=t_max,
        samples_per_row=samples_per_row,
        row_stride=row_stride,
        seed=19,
        workers=load_workers or _DEFAULT_LOAD_WORKERS,
    )
    mean_model = lgb.Booster(model_file=str(paths["mean"]))
    variance_model = lgb.Booster(model_file=str(paths["logvar"]))
    mu = _clip_mean(mean_model.predict(X))
    var = np.exp(np.clip(variance_model.predict(X), math.log(1e-6), math.log(0.25)))
    var = np.minimum(var, mu * (1.0 - mu) * 0.995)
    out = {
        "task": "predict_up_beta_continuous_t",
        "time_model": "continuous",
        "metrics": _metrics(y, mu, var),
        "mean_model_path": str(paths["mean"]),
        "variance_model_path": str(paths["logvar"]),
    }
    paths["eval"].write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Continuous-time Beta Up-price density")
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--t-min", type=float, default=0.5)
    parser.add_argument("--t-max", type=float, default=30.0)
    parser.add_argument("--samples-per-row", type=int, default=1)
    parser.add_argument("--row-stride", type=int, default=4)
    parser.add_argument("--workers", type=int, default=_DEFAULT_LOAD_WORKERS)
    parser.add_argument("--max-markets", type=int, default=None)
    parser.add_argument("--train-ratio", type=float, default=0.9)
    args = parser.parse_args(argv)
    if args.train:
        result = train_beta_continuous(
            t_min=args.t_min,
            t_max=args.t_max,
            samples_per_row=args.samples_per_row,
            row_stride=args.row_stride,
            max_markets=args.max_markets,
            train_ratio=args.train_ratio,
            load_workers=args.workers,
        )
        print(json.dumps(result["metrics"]["test"], indent=2))
        return 0
    if args.evaluate:
        print(
            json.dumps(
                evaluate_beta_continuous(
                    t_min=args.t_min,
                    t_max=args.t_max,
                    samples_per_row=args.samples_per_row,
                    row_stride=args.row_stride,
                    max_markets=args.max_markets,
                    train_ratio=args.train_ratio,
                    load_workers=args.workers,
                ),
                indent=2,
            )
        )
        return 0
    parser.error("Specify --train or --evaluate")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
