"""Train + evaluate Up-mid T-ahead LightGBM models on fetch_live data.

Horizons: 1s / 3s / 5s / 10s
Split: chronological whole-market 90% train / 10% test

Usage (from poly-monitor/backend with PYTHONPATH set):

  python -m app.ml.train_predict_up --train --horizons 1,3,5,10 --train-ratio 0.9
  python -m app.ml.train_predict_up --evaluate --horizon 5
  python -m app.ml.train_predict_up --evaluate --horizons 1,3,5,10

Or with a smaller smoke run:

  python -m app.ml.train_predict_up --train --horizons 5 --max-markets 80 --train-ratio 0.9
  python -m app.ml.train_predict_up --evaluate --horizon 5 --max-markets 80
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.data import DIRECTION_FEATURE_COLUMNS, FEATURE_COLUMNS
from app.core.live_dataset import iter_live_market_metas, live_data_root
from app.ml.live_features import load_live_feature_frame

ProgressCb = Callable[[dict[str, Any]], None]

DEFAULT_HORIZONS: tuple[float, ...] = (1.0, 3.0, 5.0, 10.0)

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


def _horizon_tag(horizon_seconds: float) -> str:
    h = float(horizon_seconds)
    if abs(h - round(h)) < 1e-9:
        return str(int(round(h)))
    return str(h).replace(".", "p")


def model_filename(horizon_seconds: float) -> str:
    return f"predict_up_h{_horizon_tag(horizon_seconds)}.txt"


def metrics_filename(horizon_seconds: float) -> str:
    return f"predict_up_h{_horizon_tag(horizon_seconds)}_metrics.json"


def eval_filename(horizon_seconds: float) -> str:
    return f"predict_up_h{_horizon_tag(horizon_seconds)}_eval.json"


def direction_model_filename(horizon_seconds: float) -> str:
    return f"predict_up_direction_h{_horizon_tag(horizon_seconds)}.txt"


def direction_metrics_filename(horizon_seconds: float) -> str:
    return f"predict_up_direction_h{_horizon_tag(horizon_seconds)}_metrics.json"


def direction_eval_filename(horizon_seconds: float) -> str:
    return f"predict_up_direction_h{_horizon_tag(horizon_seconds)}_eval.json"


def list_live_markets(*, max_markets: int | None = None) -> list[dict[str, Any]]:
    """Closed live markets under FETCH_LIVE_DATA_DIR, chronological by start_time."""
    rows: list[dict[str, Any]] = []
    for meta in iter_live_market_metas():
        if not meta.get("closed") and meta.get("winner") is None:
            continue
        st = meta.get("start_time")
        et = meta.get("end_time")
        if st is None:
            continue
        rows.append(
            {
                "market_id": str(meta["market_id"]),
                "dir": meta.get("dir"),
                "start_time": int(st),
                "end_time": int(et) if et is not None else None,
            }
        )
    rows.sort(key=lambda r: (int(r["start_time"]), str(r["market_id"])))
    if max_markets is not None:
        rows = rows[: max(0, int(max_markets))]
    return rows


def chronological_split(
    markets: list[dict[str, Any]], *, train_ratio: float = 0.9
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    n = len(markets)
    if n < 2:
        return markets, []
    cut = max(1, min(n - 1, int(round(n * float(train_ratio)))))
    return markets[:cut], markets[cut:]


def _inner_val_split(
    train_markets: list[dict[str, Any]], *, val_frac: float = 0.12
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    n = len(train_markets)
    if n < 5:
        return train_markets, train_markets[-1:] if train_markets else []
    n_val = max(1, int(round(n * float(val_frac))))
    return train_markets[:-n_val], train_markets[-n_val:]


def _build_xy_for_market(
    market: dict[str, Any],
    *,
    horizon_seconds: float,
    feature_columns: Sequence[str] = FEATURE_COLUMNS,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    """Return X, y_future_up_mid, current_up_mid, timestamps for in-window rows.

    Labels purge any row whose horizon crosses market end_time.
    """
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
        target_series = pd.to_numeric(df["up_mid"], errors="coerce").to_numpy(dtype=np.float64)
    elif "up_price" in df.columns:
        target_series = pd.to_numeric(df["up_price"], errors="coerce").to_numpy(dtype=np.float64)
    else:
        return None

    ts = pd.to_numeric(df["timestamp"], errors="coerce").to_numpy(dtype=np.int64)
    horizon_ms = int(round(float(horizon_seconds) * 1000))
    idx = np.searchsorted(ts, ts + horizon_ms, side="left")
    y = np.full(len(df), np.nan, dtype=np.float64)
    valid = idx < len(df)
    y[valid] = target_series[idx[valid]]

    # In-window only.
    in_window = np.ones(len(df), dtype=bool)
    if start_ms is not None:
        in_window &= ts >= int(start_ms)
    if end_ms is not None:
        in_window &= ts < int(end_ms)
        # Purge labels that would cross the market close.
        y = np.where(ts + horizon_ms <= int(end_ms), y, np.nan)

    feat = {}
    for col in feature_columns:
        if col in df.columns:
            feat[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            feat[col] = np.nan
    X_df = pd.DataFrame(feat)
    current = target_series.copy()

    mask = (~np.isnan(y)) & in_window & np.isfinite(current)
    if not mask.any():
        return None
    cov = X_df.loc[mask].notna().mean(axis=1)
    mask_idx = np.where(mask)[0]
    keep = cov.to_numpy() >= 0.25
    if not keep.any():
        return None
    sel = mask_idx[keep]
    X = X_df.iloc[sel][list(feature_columns)].to_numpy(dtype=np.float32)
    yy = y[sel].astype(np.float32)
    cur = current[sel].astype(np.float32)
    tsel = ts[sel].astype(np.int64)
    return X, yy, cur, tsel


def _stack_markets(
    markets: list[dict[str, Any]],
    *,
    horizon_seconds: float,
    progress_cb: ProgressCb | None = None,
    phase_label: str = "load",
    progress_lo: float = 0,
    progress_hi: float = 100,
    feature_columns: Sequence[str] = FEATURE_COLUMNS,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Stack X, y, current_mid; also return per-row market_id list."""
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    cs: list[np.ndarray] = []
    mids: list[str] = []
    total = max(1, len(markets))
    for i, m in enumerate(markets):
        built = _build_xy_for_market(
            m, horizon_seconds=horizon_seconds, feature_columns=feature_columns
        )
        if built is not None:
            X, y, cur, _ts = built
            xs.append(X)
            ys.append(y)
            cs.append(cur)
            mids.extend([str(m["market_id"])] * len(y))
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
        empty_x = np.zeros((0, len(feature_columns)), dtype=np.float32)
        empty = np.zeros((0,), dtype=np.float32)
        return empty_x, empty, empty, []
    return np.vstack(xs), np.concatenate(ys), np.concatenate(cs), mids


def _basic_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    if len(y_true) == 0:
        return {"mae": float("nan"), "rmse": float("nan"), "median_abs_error": float("nan")}
    err = np.abs(y_pred.astype(np.float64) - y_true.astype(np.float64))
    sq = (y_pred.astype(np.float64) - y_true.astype(np.float64)) ** 2
    return {
        "mae": float(np.mean(err)),
        "rmse": float(np.sqrt(np.mean(sq))),
        "median_abs_error": float(np.median(err)),
    }


def precision_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    *,
    current_mid: np.ndarray | None = None,
) -> dict[str, Any]:
    """MAE/RMSE/median + within-¢ hit rates + optional direction accuracy."""
    out: dict[str, Any] = {"n": int(len(y_true))}
    out.update(_basic_metrics(y_true, y_pred))
    if len(y_true) == 0:
        out.update(
            {
                "within_1c": float("nan"),
                "within_2c": float("nan"),
                "within_5c": float("nan"),
                "direction_acc": float("nan"),
            }
        )
        return out
    abs_err = np.abs(y_pred.astype(np.float64) - y_true.astype(np.float64))
    out["within_1c"] = float(np.mean(abs_err <= 0.01))
    out["within_2c"] = float(np.mean(abs_err <= 0.02))
    out["within_5c"] = float(np.mean(abs_err <= 0.05))
    if current_mid is not None and len(current_mid) == len(y_true):
        pred_d = y_pred.astype(np.float64) - current_mid.astype(np.float64)
        act_d = y_true.astype(np.float64) - current_mid.astype(np.float64)
        # Ignore near-zero moves for direction score.
        movable = (np.abs(act_d) >= 1e-4) | (np.abs(pred_d) >= 1e-4)
        if movable.any():
            same = np.sign(pred_d[movable]) == np.sign(act_d[movable])
            # Treat both flat (0) as match.
            both_flat = (np.abs(pred_d[movable]) < 1e-8) & (np.abs(act_d[movable]) < 1e-8)
            out["direction_acc"] = float(np.mean(same | both_flat))
            out["direction_n"] = int(movable.sum())
        else:
            out["direction_acc"] = float("nan")
            out["direction_n"] = 0
    else:
        out["direction_acc"] = float("nan")
    return out


def format_precision_report(horizon_seconds: float, metrics: dict[str, Any]) -> str:
    def pct(x: Any) -> str:
        try:
            return f"{100.0 * float(x):.1f}%"
        except (TypeError, ValueError):
            return "n/a"

    def num(x: Any, digits: int = 4) -> str:
        try:
            v = float(x)
            if np.isnan(v):
                return "n/a"
            return f"{v:.{digits}f}"
        except (TypeError, ValueError):
            return "n/a"

    lines = [
        (
            f"horizon={horizon_seconds:g}s  n={metrics.get('n', 0)}  "
            f"MAE={num(metrics.get('mae'))}  RMSE={num(metrics.get('rmse'))}  "
            f"med={num(metrics.get('median_abs_error'))}"
        ),
        (
            f"within_1c={pct(metrics.get('within_1c'))}  "
            f"within_2c={pct(metrics.get('within_2c'))}  "
            f"within_5c={pct(metrics.get('within_5c'))}"
        ),
        f"direction_acc={pct(metrics.get('direction_acc'))}",
    ]
    return "\n".join(lines)


def _default_model_path(horizon_seconds: float) -> Path:
    return settings.models_dir / model_filename(horizon_seconds)


def train_predict_up_horizon(
    *,
    horizon_seconds: float = 5.0,
    train_ratio: float = 0.9,
    num_boost_round: int = 400,
    early_stopping_rounds: int = 40,
    max_markets: int | None = None,
    params: dict[str, Any] | None = None,
    progress_cb: ProgressCb | None = None,
    markets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Train one horizon model; write predict_up_hN.txt + metrics JSON."""
    live_root = live_data_root()
    _emit(progress_cb, phase="split", progress=1, message=f"Listing markets under {live_root}…")
    all_markets = markets if markets is not None else list_live_markets(max_markets=max_markets)
    if len(all_markets) < 2:
        raise RuntimeError(
            f"Need at least 2 closed live markets under {live_root}; found {len(all_markets)}"
        )
    train_all, test_markets = chronological_split(all_markets, train_ratio=train_ratio)
    fit_markets, val_markets = _inner_val_split(train_all, val_frac=0.12)
    _emit(
        progress_cb,
        phase="split",
        progress=5,
        message=(
            f"h={horizon_seconds:g}s  {len(train_all)} train "
            f"({len(fit_markets)} fit / {len(val_markets)} val), "
            f"{len(test_markets)} test · ratio={train_ratio}"
        ),
        n_train=len(train_all),
        n_test=len(test_markets),
    )

    X_fit, y_fit, _c_fit, _ = _stack_markets(
        fit_markets,
        horizon_seconds=horizon_seconds,
        progress_cb=progress_cb,
        phase_label="fit",
        progress_lo=5,
        progress_hi=35,
    )
    X_val, y_val, _c_val, _ = _stack_markets(
        val_markets,
        horizon_seconds=horizon_seconds,
        progress_cb=progress_cb,
        phase_label="val",
        progress_lo=35,
        progress_hi=45,
    )
    X_test, y_test, c_test, _ = _stack_markets(
        test_markets,
        horizon_seconds=horizon_seconds,
        progress_cb=progress_cb,
        phase_label="test",
        progress_lo=45,
        progress_hi=55,
    )
    if len(y_fit) < 100:
        raise RuntimeError(f"Too few labeled training rows for h={horizon_seconds:g}s: {len(y_fit)}")

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
                message=f"h={horizon_seconds:g}s boosting {it}/{rounds}",
                iteration=it,
            )

    _emit(progress_cb, phase="train", progress=55, message=f"Starting LightGBM h={horizon_seconds:g}s…")
    model = lgb.train(
        boost_params,
        dtrain,
        num_boost_round=rounds,
        valid_sets=[dtrain, dval],
        valid_names=["train", "valid"],
        callbacks=[
            lgb.early_stopping(max(1, int(early_stopping_rounds)), verbose=False),
            _ProgressCallback(),
        ],
    )

    best_it = int(model.best_iteration or rounds)
    pred_fit = model.predict(X_fit, num_iteration=best_it)
    pred_val = model.predict(X_val, num_iteration=best_it) if len(y_val) else np.array([])
    pred_test = model.predict(X_test, num_iteration=best_it) if len(y_test) else np.array([])

    test_precision = (
        precision_metrics(y_test, pred_test, current_mid=c_test) if len(y_test) else {}
    )
    metrics: dict[str, Any] = {
        "task": "predict_up_mid_t_ahead",
        "data_source": "fetch_live",
        "data_root": str(live_root),
        "horizon_seconds": float(horizon_seconds),
        "train_ratio": float(train_ratio),
        "best_iteration": best_it,
        "n_features": len(FEATURE_COLUMNS),
        "n_markets": {
            "all": len(all_markets),
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
        "train": _basic_metrics(y_fit, pred_fit),
        "validation": _basic_metrics(y_val, pred_val) if len(y_val) else {},
        "test": test_precision,
        "test_market_ids": [m["market_id"] for m in test_markets],
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

    models_dir = settings.models_dir
    models_dir.mkdir(parents=True, exist_ok=True)
    model_path = models_dir / model_filename(horizon_seconds)
    model.save_model(str(model_path))
    (models_dir / "predict_up_feature_names.json").write_text(
        json.dumps(list(FEATURE_COLUMNS), indent=2), encoding="utf-8"
    )
    (models_dir / metrics_filename(horizon_seconds)).write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )
    # Convenience: also dump eval-shaped report from the train-time test split.
    (models_dir / eval_filename(horizon_seconds)).write_text(
        json.dumps(
            {
                "horizon_seconds": float(horizon_seconds),
                "source": "train_time_test_split",
                "metrics": test_precision,
                "report": format_precision_report(horizon_seconds, test_precision),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    _emit(progress_cb, phase="save", progress=100, message=f"Saved {model_path.name}", metrics=metrics)
    print(format_precision_report(horizon_seconds, test_precision))
    return {
        "model": model,
        "model_path": str(model_path),
        "metrics": metrics,
        "feature_columns": list(FEATURE_COLUMNS),
        "test_markets": test_markets,
    }


def train_predict_up(
    *,
    horizons: Sequence[float] = DEFAULT_HORIZONS,
    train_ratio: float = 0.9,
    num_boost_round: int = 400,
    early_stopping_rounds: int = 40,
    max_markets: int | None = None,
    params: dict[str, Any] | None = None,
    progress_cb: ProgressCb | None = None,
) -> dict[str, Any]:
    """Train models for each horizon with a shared chronological market list."""
    markets = list_live_markets(max_markets=max_markets)
    results: dict[str, Any] = {"horizons": {}, "n_markets": len(markets)}
    for h in horizons:
        print(f"\n=== Training horizon {h:g}s ===")
        results["horizons"][str(h)] = train_predict_up_horizon(
            horizon_seconds=float(h),
            train_ratio=train_ratio,
            num_boost_round=num_boost_round,
            early_stopping_rounds=early_stopping_rounds,
            max_markets=max_markets,
            params=params,
            progress_cb=progress_cb,
            markets=markets,
        )
        # Drop heavy booster object from aggregate summary copy.
        results["horizons"][str(h)] = {
            "model_path": results["horizons"][str(h)]["model_path"],
            "metrics": results["horizons"][str(h)]["metrics"],
        }
    summary_path = settings.models_dir / "predict_up_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def evaluate_predict_up(
    *,
    horizon_seconds: float,
    model_path: Path | str | None = None,
    market_ids: list[str] | None = None,
    max_markets: int | None = None,
    train_ratio: float = 0.9,
    progress_cb: ProgressCb | None = None,
) -> dict[str, Any]:
    """Score a saved model on held-out markets; print + save precision metrics."""
    path = Path(model_path) if model_path else _default_model_path(horizon_seconds)
    if not path.is_file():
        raise FileNotFoundError(f"Model not found: {path}")

    booster = lgb.Booster(model_file=str(path))
    all_markets = list_live_markets(max_markets=max_markets)
    if market_ids is not None:
        want = {str(x) for x in market_ids}
        test_markets = [m for m in all_markets if str(m["market_id"]) in want]
    else:
        _train, test_markets = chronological_split(all_markets, train_ratio=train_ratio)

    if not test_markets:
        raise RuntimeError("No test markets to evaluate")

    X, y, current, mids = _stack_markets(
        test_markets,
        horizon_seconds=horizon_seconds,
        progress_cb=progress_cb,
        phase_label="eval",
        progress_lo=0,
        progress_hi=90,
    )
    if len(y) == 0:
        raise RuntimeError("No labeled rows on test markets")

    pred = booster.predict(X)
    metrics = precision_metrics(y, pred, current_mid=current)

    # Per-market MAE (best/worst).
    per_market: list[dict[str, Any]] = []
    if mids:
        mid_arr = np.asarray(mids)
        for mid in sorted(set(mids)):
            sel = mid_arr == mid
            if not sel.any():
                continue
            pm = _basic_metrics(y[sel], pred[sel])
            pm["market_id"] = mid
            pm["n"] = int(sel.sum())
            per_market.append(pm)
        per_market.sort(key=lambda r: float(r.get("mae") or 0.0))
        metrics["best_markets"] = per_market[:5]
        metrics["worst_markets"] = list(reversed(per_market[-5:]))

    report = format_precision_report(horizon_seconds, metrics)
    print(report)

    out = {
        "horizon_seconds": float(horizon_seconds),
        "model_path": str(path),
        "n_test_markets": len(test_markets),
        "test_market_ids": [m["market_id"] for m in test_markets],
        "metrics": metrics,
        "report": report,
    }
    out_path = settings.models_dir / eval_filename(horizon_seconds)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    _emit(progress_cb, phase="done", progress=100, message=f"Wrote {out_path.name}", metrics=metrics)
    print(f"Wrote {out_path}")
    return out


def evaluate_predict_up_horizons(
    *,
    horizons: Sequence[float] = DEFAULT_HORIZONS,
    max_markets: int | None = None,
    train_ratio: float = 0.9,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for h in horizons:
        print(f"\n=== Evaluate horizon {h:g}s ===")
        results[str(h)] = evaluate_predict_up(
            horizon_seconds=float(h),
            max_markets=max_markets,
            train_ratio=train_ratio,
        )
    return results


def _binary_direction_metrics(y_true: np.ndarray, probability_up: np.ndarray) -> dict[str, Any]:
    """Classification accuracy and selective precision for a direction model."""
    y = y_true.astype(np.int8)
    p = probability_up.astype(np.float64)
    if not len(y):
        return {"n": 0, "accuracy": float("nan"), "auc": float("nan")}
    predicted_up = p >= 0.5
    out: dict[str, Any] = {
        "n": int(len(y)),
        "positive_rate": float(np.mean(y)),
        "accuracy": float(np.mean(predicted_up == y)),
        "binary_logloss": float(
            -np.mean(y * np.log(np.clip(p, 1e-8, 1 - 1e-8)) + (1 - y) * np.log(np.clip(1 - p, 1e-8, 1 - 1e-8)))
        ),
    }
    # Rank-based AUC; ties receive their average rank.
    n_pos, n_neg = int(y.sum()), int(len(y) - y.sum())
    if n_pos and n_neg:
        ranks = pd.Series(p).rank(method="average").to_numpy()
        out["auc"] = float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))
    else:
        out["auc"] = float("nan")
    confidence = np.abs(p - 0.5)
    for fraction in (0.1, 0.2):
        n = max(1, int(np.ceil(len(y) * fraction)))
        selected = np.argsort(confidence)[-n:]
        out[f"top_{int(fraction * 100)}pct_n"] = int(n)
        out[f"top_{int(fraction * 100)}pct_accuracy"] = float(
            np.mean(predicted_up[selected] == y[selected])
        )
    return out


def _format_direction_report(horizon_seconds: float, metrics: dict[str, Any]) -> str:
    def pct(key: str) -> str:
        value = metrics.get(key)
        return "n/a" if value is None or not np.isfinite(value) else f"{100 * float(value):.1f}%"

    auc = metrics.get("auc")
    auc_text = "n/a" if auc is None or not np.isfinite(auc) else f"{float(auc):.3f}"
    return (
        f"direction horizon={horizon_seconds:g}s n={metrics.get('n', 0)} "
        f"accuracy={pct('accuracy')} AUC={auc_text} "
        f"top10={pct('top_10pct_accuracy')} top20={pct('top_20pct_accuracy')}"
    )


def _direction_labels(
    future_mid: np.ndarray, current_mid: np.ndarray, *, min_move: float
) -> tuple[np.ndarray, np.ndarray]:
    delta = future_mid.astype(np.float64) - current_mid.astype(np.float64)
    keep = np.isfinite(delta) & (np.abs(delta) >= max(0.0, float(min_move)))
    return (delta[keep] > 0).astype(np.float32), keep


def train_predict_up_direction_horizon(
    *,
    horizon_seconds: float = 3.0,
    min_move: float = 0.001,
    train_ratio: float = 0.9,
    num_boost_round: int = 400,
    early_stopping_rounds: int = 40,
    max_markets: int | None = None,
    params: dict[str, Any] | None = None,
    progress_cb: ProgressCb | None = None,
    markets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Train a binary Up-vs-Down move model on the compact causal feature set."""
    live_root = live_data_root()
    _emit(progress_cb, phase="split", progress=1, message=f"Listing markets under {live_root}…")
    all_markets = markets if markets is not None else list_live_markets(max_markets=max_markets)
    if len(all_markets) < 2:
        raise RuntimeError("Need at least two closed live markets to train")
    train_all, test_markets = chronological_split(all_markets, train_ratio=train_ratio)
    fit_markets, val_markets = _inner_val_split(train_all)
    _emit(
        progress_cb,
        phase="split",
        progress=5,
        message=(
            f"direction h={horizon_seconds:g}s  {len(train_all)} train "
            f"({len(fit_markets)} fit / {len(val_markets)} val), "
            f"{len(test_markets)} test · min_move={min_move:g}"
        ),
    )

    def load(rows: list[dict[str, Any]], label: str, lo: float, hi: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        X, future, current, _ = _stack_markets(
            rows, horizon_seconds=horizon_seconds, feature_columns=DIRECTION_FEATURE_COLUMNS,
            progress_cb=progress_cb, phase_label=label, progress_lo=lo, progress_hi=hi,
        )
        labels, keep = _direction_labels(future, current, min_move=min_move)
        return X[keep], labels, current[keep]

    X_fit, y_fit, _ = load(fit_markets, "direction fit", 5, 35)
    X_val, y_val, _ = load(val_markets, "direction val", 35, 45)
    X_test, y_test, _ = load(test_markets, "direction test", 45, 55)
    if len(y_fit) < 100 or not y_fit.any() or y_fit.all():
        raise RuntimeError("Too few non-flat, two-sided direction labels to train")

    boost_params = {
        **DEFAULT_PARAMS,
        "objective": "binary",
        "metric": "binary_logloss",
        **(params or {}),
    }
    dtrain = lgb.Dataset(X_fit, label=y_fit, feature_name=list(DIRECTION_FEATURE_COLUMNS))
    dval = lgb.Dataset(X_val, label=y_val, reference=dtrain, feature_name=list(DIRECTION_FEATURE_COLUMNS))
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
                message=f"direction h={horizon_seconds:g}s boosting {it}/{rounds}",
                iteration=it,
            )

    _emit(
        progress_cb,
        phase="train",
        progress=55,
        message=(
            f"Starting LightGBM direction h={horizon_seconds:g}s "
            f"({len(y_fit)} train / {len(y_val)} val / {len(y_test)} test rows)…"
        ),
    )
    model = lgb.train(
        boost_params,
        dtrain,
        num_boost_round=rounds,
        valid_sets=[dval],
        valid_names=["valid"],
        callbacks=[
            lgb.early_stopping(max(1, int(early_stopping_rounds)), verbose=False),
            _ProgressCallback(),
        ],
    )
    best_it = int(model.best_iteration or num_boost_round)
    pred_fit = model.predict(X_fit, num_iteration=best_it)
    pred_val = model.predict(X_val, num_iteration=best_it)
    pred_test = model.predict(X_test, num_iteration=best_it)
    metrics: dict[str, Any] = {
        "task": "predict_up_direction_t_ahead",
        "horizon_seconds": float(horizon_seconds),
        "min_move": float(min_move),
        "train_ratio": float(train_ratio),
        "best_iteration": best_it,
        "n_features": len(DIRECTION_FEATURE_COLUMNS),
        "n_rows": {"train": int(len(y_fit)), "validation": int(len(y_val)), "test": int(len(y_test))},
        "train": _binary_direction_metrics(y_fit, pred_fit),
        "validation": _binary_direction_metrics(y_val, pred_val),
        "test": _binary_direction_metrics(y_test, pred_test),
        "test_market_ids": [m["market_id"] for m in test_markets],
        "params": boost_params,
    }
    importance = sorted(
        zip(DIRECTION_FEATURE_COLUMNS, model.feature_importance(importance_type="gain").tolist()),
        key=lambda row: row[1], reverse=True,
    )
    metrics["feature_importance_gain"] = [{"feature": n, "gain": float(g)} for n, g in importance]
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    model_path = settings.models_dir / direction_model_filename(horizon_seconds)
    model.save_model(str(model_path))
    (settings.models_dir / direction_metrics_filename(horizon_seconds)).write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )
    print(_format_direction_report(horizon_seconds, metrics["test"]), flush=True)
    _emit(progress_cb, phase="save", progress=100, message=f"Saved {model_path.name}", metrics=metrics)
    return {"model": model, "model_path": str(model_path), "metrics": metrics}


def evaluate_predict_up_direction(
    *, horizon_seconds: float, min_move: float = 0.001, model_path: Path | str | None = None,
    max_markets: int | None = None, train_ratio: float = 0.9,
) -> dict[str, Any]:
    """Evaluate a saved direction model on the chronological held-out markets."""
    path = Path(model_path) if model_path else settings.models_dir / direction_model_filename(horizon_seconds)
    if not path.is_file():
        raise FileNotFoundError(f"Direction model not found: {path}")
    _train, test_markets = chronological_split(list_live_markets(max_markets=max_markets), train_ratio=train_ratio)
    X, future, current, _ = _stack_markets(
        test_markets, horizon_seconds=horizon_seconds, feature_columns=DIRECTION_FEATURE_COLUMNS
    )
    y, keep = _direction_labels(future, current, min_move=min_move)
    metrics = _binary_direction_metrics(y, lgb.Booster(model_file=str(path)).predict(X[keep]))
    out = {"task": "predict_up_direction_t_ahead", "horizon_seconds": horizon_seconds, "min_move": min_move, "model_path": str(path), "metrics": metrics}
    (settings.models_dir / direction_eval_filename(horizon_seconds)).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(_format_direction_report(horizon_seconds, metrics), flush=True)
    return out


def _parse_horizons(raw: str | None) -> list[float]:
    if not raw:
        return list(DEFAULT_HORIZONS)
    out: list[float] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    if not out:
        raise SystemExit("No horizons provided")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Train / evaluate predict_up level or direction models")
    p.add_argument("--train", action="store_true", help="Train model(s)")
    p.add_argument("--evaluate", action="store_true", help="Evaluate saved model(s) on 10% test markets")
    p.add_argument("--horizon", type=float, default=None, help="Single horizon seconds")
    p.add_argument("--horizons", type=str, default=None, help="Comma list, e.g. 1,3,5,10")
    p.add_argument("--train-ratio", type=float, default=0.9)
    p.add_argument("--max-markets", type=int, default=None)
    p.add_argument("--num-boost-round", type=int, default=400)
    p.add_argument("--early-stopping-rounds", type=int, default=40)
    p.add_argument("--model-path", type=str, default=None)
    p.add_argument(
        "--task",
        choices=("level", "direction"),
        default="direction",
        help="direction predicts a non-flat Up move; level predicts future Up mid",
    )
    p.add_argument(
        "--min-move",
        type=float,
        default=0.001,
        help="minimum absolute Up-mid move to label for --task direction (price units)",
    )
    args = p.parse_args(argv)

    if not args.train and not args.evaluate:
        p.print_help()
        return 2

    if args.horizon is not None:
        horizons = [float(args.horizon)]
    else:
        horizons = _parse_horizons(args.horizons)

    def _progress(payload: dict[str, Any]) -> None:
        msg = payload.get("message")
        if not msg:
            return
        phase = payload.get("phase")
        if phase in {"split", "train", "save", "done"}:
            print(msg, flush=True)
            return
        if phase != "load":
            return
        # Throttle market-load spam: first, last, every 25 markets, or ~every 5%.
        loaded = int(payload.get("loaded") or 0)
        total = max(1, int(payload.get("total") or 1))
        if loaded in {1, total} or loaded % 25 == 0 or (loaded * 20) % total < 20:
            print(msg, flush=True)

    if args.task == "direction":
        if args.train:
            for h in horizons:
                print(f"\n=== Training direction horizon {h:g}s ===", flush=True)
                train_predict_up_direction_horizon(
                    horizon_seconds=h,
                    min_move=args.min_move,
                    train_ratio=args.train_ratio,
                    num_boost_round=args.num_boost_round,
                    early_stopping_rounds=args.early_stopping_rounds,
                    max_markets=args.max_markets,
                    progress_cb=_progress,
                )
        if args.evaluate:
            for h in horizons:
                evaluate_predict_up_direction(
                    horizon_seconds=h,
                    min_move=args.min_move,
                    model_path=args.model_path,
                    max_markets=args.max_markets,
                    train_ratio=args.train_ratio,
                )
        return 0

    if args.train:
        if len(horizons) == 1:
            train_predict_up_horizon(
                horizon_seconds=horizons[0],
                train_ratio=args.train_ratio,
                num_boost_round=args.num_boost_round,
                early_stopping_rounds=args.early_stopping_rounds,
                max_markets=args.max_markets,
                progress_cb=_progress,
            )
        else:
            train_predict_up(
                horizons=horizons,
                train_ratio=args.train_ratio,
                num_boost_round=args.num_boost_round,
                early_stopping_rounds=args.early_stopping_rounds,
                max_markets=args.max_markets,
                progress_cb=_progress,
            )

    if args.evaluate:
        for h in horizons:
            evaluate_predict_up(
                horizon_seconds=h,
                model_path=args.model_path,
                max_markets=args.max_markets,
                train_ratio=args.train_ratio,
                progress_cb=_progress,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
