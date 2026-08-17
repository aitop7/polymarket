"""Train/evaluate a separate heteroscedastic Beta model for future Up price.

The existing level and direction artifacts are never overwritten.  This model
uses two LightGBM heads:
  μ(X)              future Up-price mean
  log variance(X)   conditional uncertainty learned from out-of-sample residuals

Those moments map to Beta parameters α=μκ, β=(1-μ)κ where
κ=μ(1-μ)/variance-1.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np

from app.core.config import settings
from app.core.data import FEATURE_COLUMNS
from app.ml.train_predict_up import (
    DEFAULT_PARAMS,
    _emit,
    _inner_val_split,
    _stack_markets,
    chronological_split,
    list_live_markets,
)


def _tag(horizon: float) -> str:
    return f"{float(horizon):g}".replace(".", "p")


def beta_mean_filename(horizon: float) -> str:
    return f"predict_up_beta_mean_h{_tag(horizon)}.txt"


def beta_logvar_filename(horizon: float) -> str:
    return f"predict_up_beta_logvar_h{_tag(horizon)}.txt"


def beta_metrics_filename(horizon: float) -> str:
    return f"predict_up_beta_h{_tag(horizon)}_metrics.json"


def beta_eval_filename(horizon: float) -> str:
    return f"predict_up_beta_h{_tag(horizon)}_eval.json"


def _clip_mean(value: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(value, dtype=np.float64), 1e-4, 1.0 - 1e-4)


def _moments_to_beta(mean: np.ndarray, variance: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mu = _clip_mean(mean)
    # A Beta variance cannot exceed μ(1-μ). Keep concentration finite and stable.
    maximum = mu * (1.0 - mu) * 0.995
    var = np.clip(np.asarray(variance, dtype=np.float64), 1e-6, maximum)
    concentration = np.clip(mu * (1.0 - mu) / var - 1.0, 2.0, 400.0)
    return mu * concentration, (1.0 - mu) * concentration


def _beta_nll(y: np.ndarray, alpha: np.ndarray, beta: np.ndarray) -> float:
    target = np.clip(np.asarray(y, dtype=np.float64), 1e-5, 1.0 - 1e-5)
    log_beta = np.fromiter(
        (math.lgamma(float(a)) + math.lgamma(float(b)) - math.lgamma(float(a + b)) for a, b in zip(alpha, beta)),
        dtype=np.float64,
        count=len(target),
    )
    ll = (alpha - 1.0) * np.log(target) + (beta - 1.0) * np.log1p(-target) - log_beta
    return float(-np.mean(ll))


def _metrics(y: np.ndarray, mean: np.ndarray, variance: np.ndarray) -> dict[str, float]:
    target = np.asarray(y, dtype=np.float64)
    mu = _clip_mean(mean)
    var = np.asarray(variance, dtype=np.float64)
    alpha, beta = _moments_to_beta(mu, var)
    err = target - mu
    standardized_sq = (err * err) / np.maximum(var, 1e-8)
    return {
        "n": int(len(target)),
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err * err))),
        "beta_nll": _beta_nll(target, alpha, beta),
        "mean_predicted_variance": float(np.mean(var)),
        "mean_realized_squared_error": float(np.mean(err * err)),
        "variance_ratio": float(np.mean(standardized_sq)),
    }


def _train_regressor(
    X: np.ndarray,
    y: np.ndarray,
    *,
    rounds: int,
    seed: int,
) -> lgb.Booster:
    params = {
        **DEFAULT_PARAMS,
        "metric": "None",
        "seed": seed,
        "min_child_samples": 80,
        "num_leaves": 31,
    }
    return lgb.train(
        params,
        lgb.Dataset(X, label=y, feature_name=list(FEATURE_COLUMNS)),
        num_boost_round=max(30, int(rounds)),
    )


def train_beta_horizon(
    *,
    horizon_seconds: float = 3.0,
    train_ratio: float = 0.9,
    num_boost_round: int = 300,
    max_markets: int | None = None,
    progress_cb: Any = None,
) -> dict[str, Any]:
    """Train independent μ and log-variance heads without overwriting other models."""
    markets = list_live_markets(max_markets=max_markets)
    if len(markets) < 8:
        raise RuntimeError("Need at least eight closed markets for a Beta model")
    train_markets, test_markets = chronological_split(markets, train_ratio=train_ratio)
    fit_markets, variance_markets = _inner_val_split(train_markets, val_frac=0.2)
    _emit(progress_cb, phase="split", progress=5, message=f"Beta h={horizon_seconds:g}s: loading datasets")

    X_fit, y_fit, _, _ = _stack_markets(fit_markets, horizon_seconds=horizon_seconds, progress_cb=progress_cb)
    X_var, y_var, _, _ = _stack_markets(variance_markets, horizon_seconds=horizon_seconds, progress_cb=progress_cb)
    X_test, y_test, _, _ = _stack_markets(test_markets, horizon_seconds=horizon_seconds, progress_cb=progress_cb)
    if min(len(y_fit), len(y_var), len(y_test)) < 100:
        raise RuntimeError("Too few usable rows in chronological Beta train/validation/test split")

    _emit(progress_cb, phase="train", progress=45, message=f"Training Beta mean head h={horizon_seconds:g}s")
    mean_model = _train_regressor(X_fit, y_fit, rounds=num_boost_round, seed=71)
    mu_var = _clip_mean(mean_model.predict(X_var))
    # Residual targets are out-of-sample relative to the mean head, avoiding in-sample variance collapse.
    log_residual_var = np.log(np.maximum((y_var - mu_var) ** 2, 1e-6))

    _emit(progress_cb, phase="train", progress=70, message=f"Training Beta variance head h={horizon_seconds:g}s")
    variance_model = _train_regressor(X_var, log_residual_var, rounds=max(80, num_boost_round // 2), seed=73)

    def predict(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        mu = _clip_mean(mean_model.predict(X))
        var = np.exp(np.clip(variance_model.predict(X), math.log(1e-6), math.log(0.25)))
        max_var = mu * (1.0 - mu) * 0.995
        return mu, np.minimum(var, max_var)

    mu_fit, var_fit = predict(X_fit)
    mu_var, var_var = predict(X_var)
    mu_test, var_test = predict(X_test)
    metrics: dict[str, Any] = {
        "task": "predict_up_beta_t_ahead",
        "distribution": "Beta(alpha=mu*kappa, beta=(1-mu)*kappa)",
        "horizon_seconds": float(horizon_seconds),
        "train_ratio": float(train_ratio),
        "n_features": len(FEATURE_COLUMNS),
        "n_markets": {"fit": len(fit_markets), "variance": len(variance_markets), "test": len(test_markets)},
        "train": _metrics(y_fit, mu_fit, var_fit),
        "validation": _metrics(y_var, mu_var, var_var),
        "test": _metrics(y_test, mu_test, var_test),
    }
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    mean_path = settings.models_dir / beta_mean_filename(horizon_seconds)
    var_path = settings.models_dir / beta_logvar_filename(horizon_seconds)
    mean_model.save_model(str(mean_path))
    variance_model.save_model(str(var_path))
    (settings.models_dir / beta_metrics_filename(horizon_seconds)).write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )
    _emit(progress_cb, phase="save", progress=100, message=f"Saved Beta h={horizon_seconds:g}s", metrics=metrics)
    return {"mean_model_path": str(mean_path), "variance_model_path": str(var_path), "metrics": metrics}


def evaluate_beta_horizon(
    *, horizon_seconds: float = 3.0, train_ratio: float = 0.9, max_markets: int | None = None
) -> dict[str, Any]:
    """Evaluate saved Beta heads on the chronological held-out market partition."""
    mean_path = settings.models_dir / beta_mean_filename(horizon_seconds)
    var_path = settings.models_dir / beta_logvar_filename(horizon_seconds)
    if not mean_path.is_file() or not var_path.is_file():
        raise FileNotFoundError(f"Beta models not found for h={horizon_seconds:g}s")
    _train, test_markets = chronological_split(list_live_markets(max_markets=max_markets), train_ratio=train_ratio)
    X, y, _, _ = _stack_markets(test_markets, horizon_seconds=horizon_seconds)
    mean_model = lgb.Booster(model_file=str(mean_path))
    variance_model = lgb.Booster(model_file=str(var_path))
    mu = _clip_mean(mean_model.predict(X))
    var = np.exp(np.clip(variance_model.predict(X), math.log(1e-6), math.log(0.25)))
    var = np.minimum(var, mu * (1.0 - mu) * 0.995)
    out = {
        "task": "predict_up_beta_t_ahead",
        "horizon_seconds": float(horizon_seconds),
        "metrics": _metrics(y, mu, var),
        "mean_model_path": str(mean_path),
        "variance_model_path": str(var_path),
    }
    (settings.models_dir / beta_eval_filename(horizon_seconds)).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train/evaluate separate Beta Up-price models")
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--horizon", type=float, default=3.0)
    parser.add_argument("--max-markets", type=int, default=None)
    parser.add_argument("--train-ratio", type=float, default=0.9)
    parser.add_argument("--num-boost-round", type=int, default=300)
    args = parser.parse_args(argv)
    if not args.train and not args.evaluate:
        parser.print_help()
        return 2
    if args.train:
        result = train_beta_horizon(
            horizon_seconds=args.horizon,
            max_markets=args.max_markets,
            train_ratio=args.train_ratio,
            num_boost_round=args.num_boost_round,
        )
        print(json.dumps(result["metrics"]["test"], indent=2))
    if args.evaluate:
        print(json.dumps(evaluate_beta_horizon(
            horizon_seconds=args.horizon, max_markets=args.max_markets, train_ratio=args.train_ratio
        )["metrics"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
