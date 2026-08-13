"""Train LightGBM regression: predict UP mid T seconds ahead from live VWAP data."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.data import FEATURE_COLUMNS
from app.core.live_dataset import iter_live_market_metas, live_data_root
from app.ml.live_features import load_live_feature_frame

ProgressCb = Callable[[dict[str, Any]], None]


DEFAULT_PARAMS: dict[str, Any] = {
    "objective": "regression",
    "metric": "l2",
    "boosting_type": "gbdt",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 50,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "verbosity": -1,
    "seed": 42,
}


def _emit(cb: ProgressCb | None, **payload: Any) -> None:
    if cb is not None:
        cb(payload)


def _list_live_markets(*, max_markets: int | None = None) -> list[dict[str, Any]]:
    """Closed live markets under FETCH_LIVE_DATA_DIR, chronological by start_time."""
    rows: list[dict[str, Any]] = []
    for meta in iter_live_market_metas():
        if not meta.get("closed") and meta.get("winner") is None:
            continue
        rows.append(
            {
                "market_id": str(meta["market_id"]),
                "split_src": "twap",
                "dir": meta.get("dir"),
                "start_time": int(meta["start_time"]),
            }
        )
    rows.sort(key=lambda r: (int(r["start_time"]), str(r["market_id"])))
    if max_markets is not None:
        rows = rows[: max(0, int(max_markets))]
    return rows


def _chronological_split(
    markets: list[dict[str, Any]], *, train_ratio: float = 0.8
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    n = len(markets)
    if n < 2:
        return markets, []
    cut = max(1, min(n - 1, int(round(n * float(train_ratio)))))
    return markets[:cut], markets[cut:]


def _inner_val_split(
    train_markets: list[dict[str, Any]], *, val_frac: float = 0.15
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    n = len(train_markets)
    if n < 5:
        return train_markets, train_markets[-1:] if train_markets else []
    n_val = max(1, int(round(n * float(val_frac))))
    return train_markets[:-n_val], train_markets[-n_val:]


def _build_xy_for_market(
    market: dict[str, Any], *, horizon_seconds: float
) -> tuple[np.ndarray, np.ndarray] | None:
    try:
        df = load_live_feature_frame(
            str(market["market_id"]),
            market_dir=market.get("dir"),
        )
    except Exception:
        return None
    if df.empty or "timestamp" not in df.columns:
        return None
    df = df.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    if "up_mid" in df.columns:
        target_series = pd.to_numeric(df["up_mid"], errors="coerce").to_numpy(dtype=np.float64)
    elif "up_price" in df.columns:
        target_series = pd.to_numeric(df["up_price"], errors="coerce").to_numpy(dtype=np.float64)
    else:
        return None

    ts = pd.to_numeric(df["timestamp"], errors="coerce").to_numpy(dtype=np.int64)
    horizon_ms = int(horizon_seconds * 1000)
    idx = np.searchsorted(ts, ts + horizon_ms, side="left")
    y = np.full(len(df), np.nan, dtype=np.float64)
    valid = idx < len(df)
    y[valid] = target_series[idx[valid]]

    feat = {}
    for col in FEATURE_COLUMNS:
        if col in df.columns:
            feat[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            feat[col] = np.nan
    X_df = pd.DataFrame(feat)
    mask = ~np.isnan(y)
    # Drop rows with almost no features.
    cov = X_df.loc[mask].notna().mean(axis=1)
    mask_idx = np.where(mask)[0]
    keep = cov.to_numpy() >= 0.25
    if not keep.any():
        return None
    sel = mask_idx[keep]
    X = X_df.iloc[sel][FEATURE_COLUMNS].to_numpy(dtype=np.float32)
    yy = y[sel].astype(np.float32)
    return X, yy


def _stack_markets(
    markets: list[dict[str, Any]],
    *,
    horizon_seconds: float,
    progress_cb: ProgressCb | None,
    phase_label: str,
    progress_lo: float,
    progress_hi: float,
) -> tuple[np.ndarray, np.ndarray]:
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    total = max(1, len(markets))
    for i, m in enumerate(markets):
        built = _build_xy_for_market(m, horizon_seconds=horizon_seconds)
        if built is not None:
            xs.append(built[0])
            ys.append(built[1])
        pct = int(progress_lo + (progress_hi - progress_lo) * ((i + 1) / total))
        _emit(
            progress_cb,
            phase="load",
            progress=pct,
            message=f"{phase_label}: loaded {i + 1}/{total} markets",
            loaded=i + 1,
            total=total,
        )
    if not xs:
        return np.zeros((0, len(FEATURE_COLUMNS)), dtype=np.float32), np.zeros((0,), dtype=np.float32)
    return np.vstack(xs), np.concatenate(ys)


def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    if len(y_true) == 0:
        return {"mae": float("nan"), "rmse": float("nan")}
    err = y_pred - y_true
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err ** 2)))
    return {"mae": mae, "rmse": rmse}


def train_up_price_model(
    *,
    horizon_seconds: float = 5.0,
    train_ratio: float = 0.8,
    num_boost_round: int = 400,
    early_stopping_rounds: int = 40,
    max_markets: int | None = None,
    params: dict[str, Any] | None = None,
    progress_cb: ProgressCb | None = None,
) -> dict[str, Any]:
    live_root = live_data_root()
    _emit(
        progress_cb,
        phase="split",
        progress=1,
        message=f"Listing live markets under {live_root}…",
    )
    markets = _list_live_markets(max_markets=max_markets)
    if len(markets) < 2:
        raise RuntimeError(
            f"Need at least 2 closed live markets under {live_root}; found {len(markets)}"
        )
    train_all, test_markets = _chronological_split(markets, train_ratio=train_ratio)
    fit_markets, val_markets = _inner_val_split(train_all, val_frac=0.15)
    _emit(
        progress_cb,
        phase="split",
        progress=5,
        message=(
            f"Live VWAP 80/20: {len(train_all)} train ({len(fit_markets)} fit / "
            f"{len(val_markets)} early-stop val), {len(test_markets)} test · {live_root}"
        ),
        n_train=len(train_all),
        n_test=len(test_markets),
        data_root=str(live_root),
    )

    X_fit, y_fit = _stack_markets(
        fit_markets,
        horizon_seconds=horizon_seconds,
        progress_cb=progress_cb,
        phase_label="fit",
        progress_lo=5,
        progress_hi=35,
    )
    X_val, y_val = _stack_markets(
        val_markets,
        horizon_seconds=horizon_seconds,
        progress_cb=progress_cb,
        phase_label="val",
        progress_lo=35,
        progress_hi=45,
    )
    X_test, y_test = _stack_markets(
        test_markets,
        horizon_seconds=horizon_seconds,
        progress_cb=progress_cb,
        phase_label="test",
        progress_lo=45,
        progress_hi=55,
    )
    if len(y_fit) < 100:
        raise RuntimeError(f"Too few labeled training rows: {len(y_fit)}")

    boost_params = {**DEFAULT_PARAMS, **(params or {})}
    dtrain = lgb.Dataset(
        X_fit, label=y_fit, feature_name=list(FEATURE_COLUMNS), free_raw_data=False
    )
    dval = lgb.Dataset(
        X_val,
        label=y_val,
        reference=dtrain,
        feature_name=list(FEATURE_COLUMNS),
        free_raw_data=False,
    )

    rounds = max(10, int(num_boost_round))

    class _ProgressCallback:
        def __init__(self) -> None:
            self.order = 10
            self.before_iteration = False

        def __call__(self, env: Any) -> None:
            it = int(getattr(env, "iteration", 0) or 0) + 1
            pct = int(55 + 35 * min(1.0, it / max(1, rounds)))
            _emit(
                progress_cb,
                phase="train",
                progress=pct,
                message=f"Boosting iteration {it}/{rounds}",
                iteration=it,
                num_boost_round=rounds,
            )

    _emit(progress_cb, phase="train", progress=55, message="Starting LightGBM…")
    callbacks: list[Any] = [
        lgb.early_stopping(max(1, int(early_stopping_rounds)), verbose=False),
        _ProgressCallback(),
    ]
    model = lgb.train(
        boost_params,
        dtrain,
        num_boost_round=rounds,
        valid_sets=[dtrain, dval],
        valid_names=["train", "valid"],
        callbacks=callbacks,
    )

    _emit(progress_cb, phase="eval", progress=92, message="Evaluating…")
    best_it = int(model.best_iteration or rounds)
    preds = {
        "train": model.predict(X_fit, num_iteration=best_it),
        "validation": model.predict(X_val, num_iteration=best_it) if len(y_val) else np.array([]),
        "test": model.predict(X_test, num_iteration=best_it) if len(y_test) else np.array([]),
    }
    metrics: dict[str, Any] = {
        "task": "up_mid_t_ahead",
        "data_source": "fetch_live",
        "data_root": str(live_root),
        "horizon_seconds": float(horizon_seconds),
        "train_ratio": float(train_ratio),
        "best_iteration": best_it,
        "n_features": len(FEATURE_COLUMNS),
        "n_markets": {
            "train": len(train_all),
            "fit": len(fit_markets),
            "validation": len(val_markets),
            "test": len(test_markets),
        },
        "n_rows": {
            "train": int(len(y_fit)),
            "validation": int(len(y_val)),
            "test": int(len(y_test)),
        },
        "train": _metrics(y_fit, preds["train"]),
        "validation": _metrics(y_val, preds["validation"]) if len(y_val) else {},
        "test": _metrics(y_test, preds["test"]) if len(y_test) else {},
        "params": boost_params,
    }
    importance = sorted(
        zip(
            FEATURE_COLUMNS,
            model.feature_importance(importance_type="gain").tolist(),
        ),
        key=lambda x: x[1],
        reverse=True,
    )
    metrics["feature_importance_gain_top20"] = [
        {"feature": name, "gain": float(gain)} for name, gain in importance[:20]
    ]

    _emit(progress_cb, phase="save", progress=96, message="Saving model…")
    models_dir = settings.models_dir
    models_dir.mkdir(parents=True, exist_ok=True)
    model_path = models_dir / "momentum_pair_up_mid.txt"
    model.save_model(str(model_path))
    (models_dir / "momentum_pair_feature_names.json").write_text(
        json.dumps(list(FEATURE_COLUMNS), indent=2), encoding="utf-8"
    )
    (models_dir / "momentum_pair_metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )

    _emit(progress_cb, phase="save", progress=100, message="Done", metrics=metrics)
    return {
        "model": model,
        "model_path": str(model_path),
        "metrics": metrics,
        "feature_columns": list(FEATURE_COLUMNS),
    }
