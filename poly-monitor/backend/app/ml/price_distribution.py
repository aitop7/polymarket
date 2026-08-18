"""Future Up-price probability density as a function of continuous time t.

Core API:

    future_up_price_pdf(t_seconds, features, family="beta")

Preferred path (family=\"beta\"):
  continuous-time heads μ(X,t), log σ²(X,t) trained with features
  [market features…, t, log(t), √t]  → true PDF(x | X, t) for any t > 0

Fallback:
  discrete per-horizon Beta / level artifacts with log-variance interpolation in t.
"""

from __future__ import annotations

import json
import math
import threading
from typing import Any, Literal

import lightgbm as lgb
import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.data import FEATURE_COLUMNS
from app.ml.train_predict_up import metrics_filename, model_filename
from app.ml.train_predict_up_beta import (
    _moments_to_beta,
    beta_logvar_filename,
    beta_mean_filename,
    beta_metrics_filename,
)
from app.ml.train_predict_up_beta_ct import (
    CT_METRICS_FILENAME,
    augment_features,
    continuous_model_paths,
    continuous_model_ready,
)

Family = Literal["beta", "level"]

_LOCK = threading.Lock()
_BETA_MEAN: dict[float, tuple[float, lgb.Booster]] = {}
_BETA_VAR: dict[float, tuple[float, lgb.Booster]] = {}
_LEVEL: dict[float, tuple[float, lgb.Booster]] = {}
_BETA_FLOOR: dict[float, float] = {}
_LEVEL_STD: dict[float, float] = {}
_CT_MEAN: tuple[float, lgb.Booster] | None = None
_CT_VAR: tuple[float, lgb.Booster] | None = None
_CT_FLOOR: float | None = None


def _tag(horizon: float) -> str:
    return f"{float(horizon):g}".replace(".", "p")


def _feature_vector(features: pd.Series | np.ndarray | dict[str, Any]) -> np.ndarray:
    if isinstance(features, np.ndarray):
        values = np.asarray(features, dtype=np.float32).reshape(-1)
        if values.size != len(FEATURE_COLUMNS):
            raise ValueError(f"Expected {len(FEATURE_COLUMNS)} features, got {values.size}")
        return values
    if isinstance(features, dict):
        row = pd.Series(features)
    else:
        row = features
    values = pd.to_numeric(row.reindex(FEATURE_COLUMNS), errors="coerce").to_numpy(dtype=np.float32)
    return values


def available_pdf_horizons(family: Family = "beta") -> tuple[float, ...]:
    """Horizons that have saved artifacts for the given density family."""
    if family == "beta":
        found: list[float] = []
        for path in sorted(settings.models_dir.glob("predict_up_beta_mean_h*.txt")):
            raw = path.stem.removeprefix("predict_up_beta_mean_h")
            try:
                h = float(raw.replace("p", "."))
            except ValueError:
                continue
            if (settings.models_dir / beta_logvar_filename(h)).is_file():
                found.append(h)
        return tuple(sorted(set(found)))
    found = []
    for path in sorted(settings.models_dir.glob("predict_up_h*.txt")):
        # Skip direction / beta / metrics-like names.
        name = path.stem
        if name.startswith("predict_up_direction") or name.startswith("predict_up_beta"):
            continue
        raw = name.removeprefix("predict_up_h")
        if not raw or not raw[0].isdigit():
            continue
        try:
            h = float(raw.replace("p", "."))
        except ValueError:
            continue
        found.append(h)
    return tuple(sorted(set(found)))


def _load_beta_heads(horizon: float) -> tuple[lgb.Booster, lgb.Booster]:
    h = float(horizon)
    mean_path = settings.models_dir / beta_mean_filename(h)
    var_path = settings.models_dir / beta_logvar_filename(h)
    if not mean_path.is_file() or not var_path.is_file():
        raise FileNotFoundError(f"Beta model not found for t={h:g}s")
    mean_stamp = mean_path.stat().st_mtime
    var_stamp = var_path.stat().st_mtime
    with _LOCK:
        mean_cached = _BETA_MEAN.get(h)
        var_cached = _BETA_VAR.get(h)
        if mean_cached is None or mean_cached[0] != mean_stamp:
            _BETA_MEAN[h] = (mean_stamp, lgb.Booster(model_file=str(mean_path)))
        if var_cached is None or var_cached[0] != var_stamp:
            _BETA_VAR[h] = (var_stamp, lgb.Booster(model_file=str(var_path)))
        return _BETA_MEAN[h][1], _BETA_VAR[h][1]


def _load_level_model(horizon: float) -> lgb.Booster:
    h = float(horizon)
    path = settings.models_dir / model_filename(h)
    if not path.is_file():
        raise FileNotFoundError(f"Level model not found for t={h:g}s")
    stamp = path.stat().st_mtime
    with _LOCK:
        cached = _LEVEL.get(h)
        if cached is None or cached[0] != stamp:
            _LEVEL[h] = (stamp, lgb.Booster(model_file=str(path)))
        return _LEVEL[h][1]


def _beta_residual_var(horizon: float) -> float:
    h = float(horizon)
    with _LOCK:
        cached = _BETA_FLOOR.get(h)
        if cached is not None:
            return cached
    floor = 0.04**2
    path = settings.models_dir / beta_metrics_filename(h)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        test = payload.get("test") or {}
        mse = test.get("mean_realized_squared_error")
        rmse = test.get("rmse")
        if mse is not None and float(mse) > 0:
            floor = float(mse)
        elif rmse is not None and float(rmse) > 0:
            floor = float(rmse) ** 2
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    with _LOCK:
        _BETA_FLOOR[h] = floor
    return floor


def _level_residual_std(horizon: float) -> float:
    h = float(horizon)
    with _LOCK:
        cached = _LEVEL_STD.get(h)
        if cached is not None:
            return cached
    std = 0.04
    path = settings.models_dir / metrics_filename(h)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rmse = (payload.get("test") or {}).get("rmse")
        if rmse is not None and float(rmse) > 0:
            std = float(rmse)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    with _LOCK:
        _LEVEL_STD[h] = std
    return std


def _bracket_horizons(t: float, trained: tuple[float, ...]) -> tuple[float, float, float]:
    """Return (lo, hi, weight) with weight in [0,1] for interpolation toward hi."""
    if not trained:
        raise FileNotFoundError("No trained density models available")
    ordered = sorted(float(h) for h in trained)
    target = float(t)
    if target <= ordered[0]:
        return ordered[0], ordered[0], 0.0
    if target >= ordered[-1]:
        return ordered[-1], ordered[-1], 0.0
    for lo, hi in zip(ordered, ordered[1:]):
        if abs(target - lo) < 1e-12:
            return lo, lo, 0.0
        if abs(target - hi) < 1e-12:
            return hi, hi, 0.0
        if lo < target < hi:
            return lo, hi, (target - lo) / (hi - lo)
    return ordered[-1], ordered[-1], 0.0


def _moments_at_horizon_beta(values: np.ndarray, horizon: float) -> tuple[float, float]:
    mean_model, var_model = _load_beta_heads(horizon)
    mu = float(np.clip(mean_model.predict(values.reshape(1, -1))[0], 1e-4, 1.0 - 1e-4))
    raw_var = float(
        np.exp(np.clip(var_model.predict(values.reshape(1, -1))[0], math.log(1e-6), math.log(0.25)))
    )
    floor = _beta_residual_var(horizon)
    max_var = mu * (1.0 - mu) * 0.995
    var = float(min(max(raw_var, floor), max_var))
    return mu, var


def _moments_at_horizon_level(values: np.ndarray, horizon: float) -> tuple[float, float]:
    raw = float(_load_level_model(horizon).predict(values.reshape(1, -1))[0])
    mu = float(np.clip(raw, 1e-4, 1.0 - 1e-4))
    std = max(0.005, _level_residual_std(horizon))
    return mu, float(std * std)


def _load_continuous_beta_heads() -> tuple[lgb.Booster, lgb.Booster]:
    global _CT_MEAN, _CT_VAR
    paths = continuous_model_paths()
    if not paths["mean"].is_file() or not paths["logvar"].is_file():
        raise FileNotFoundError("Continuous-t Beta model not trained yet")
    mean_stamp = paths["mean"].stat().st_mtime
    var_stamp = paths["logvar"].stat().st_mtime
    with _LOCK:
        if _CT_MEAN is None or _CT_MEAN[0] != mean_stamp:
            _CT_MEAN = (mean_stamp, lgb.Booster(model_file=str(paths["mean"])))
        if _CT_VAR is None or _CT_VAR[0] != var_stamp:
            _CT_VAR = (var_stamp, lgb.Booster(model_file=str(paths["logvar"])))
        return _CT_MEAN[1], _CT_VAR[1]


def _continuous_beta_floor() -> float:
    global _CT_FLOOR
    with _LOCK:
        if _CT_FLOOR is not None:
            return _CT_FLOOR
    floor = 0.04**2
    path = settings.models_dir / CT_METRICS_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        test = payload.get("test") or {}
        mse = test.get("mean_realized_squared_error")
        rmse = test.get("rmse")
        if mse is not None and float(mse) > 0:
            floor = float(mse)
        elif rmse is not None and float(rmse) > 0:
            floor = float(rmse) ** 2
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    with _LOCK:
        _CT_FLOOR = floor
    return floor


def _moments_continuous_beta(values: np.ndarray, t_seconds: float) -> dict[str, Any]:
    """μ(X,t), σ²(X,t) from the continuous-time Beta heads."""
    mean_model, var_model = _load_continuous_beta_heads()
    x_t = augment_features(values, float(t_seconds)).reshape(1, -1)
    mu = float(np.clip(mean_model.predict(x_t)[0], 1e-4, 1.0 - 1e-4))
    raw_var = float(np.exp(np.clip(var_model.predict(x_t)[0], math.log(1e-6), math.log(0.25))))
    floor = _continuous_beta_floor()
    max_var = mu * (1.0 - mu) * 0.995
    var = float(min(max(raw_var, floor), max_var))
    alpha_arr, beta_arr = _moments_to_beta(np.array([mu]), np.array([var]))
    return {
        "t_seconds": float(t_seconds),
        "family": "beta",
        "mean": mu,
        "variance": var,
        "std": float(math.sqrt(var)),
        "alpha": float(alpha_arr[0]),
        "beta": float(beta_arr[0]),
        "source": "beta_continuous_t",
        "time_model": "continuous",
        "trained_horizons": [],
        "bracket": {"lo": float(t_seconds), "hi": float(t_seconds), "weight": 0.0},
    }


def predict_moments(
    t_seconds: float,
    features: pd.Series | np.ndarray | dict[str, Any],
    *,
    family: Family = "beta",
) -> dict[str, Any]:
    """Predict μ and variance of Up price at continuous time t seconds ahead."""
    values = _feature_vector(features)
    t = float(t_seconds)
    if t <= 0:
        raise ValueError("t_seconds must be > 0")

    if family == "beta" and continuous_model_ready():
        return _moments_continuous_beta(values, t)

    trained = available_pdf_horizons(family)
    lo, hi, weight = _bracket_horizons(t, trained)
    exact = abs(lo - hi) < 1e-12

    if family == "beta":
        mu_lo, var_lo = _moments_at_horizon_beta(values, lo)
        if exact:
            mu, var = mu_lo, var_lo
            source = f"beta_h{lo:g}"
        else:
            mu_hi, var_hi = _moments_at_horizon_beta(values, hi)
            mu = (1.0 - weight) * mu_lo + weight * mu_hi
            log_var = (1.0 - weight) * math.log(max(var_lo, 1e-8)) + weight * math.log(max(var_hi, 1e-8))
            var = math.exp(log_var)
            source = f"beta_interp[{lo:g},{hi:g}]"
        mu = float(np.clip(mu, 1e-4, 1.0 - 1e-4))
        max_var = mu * (1.0 - mu) * 0.995
        var = float(min(max(var, 1e-6), max_var))
        alpha_arr, beta_arr = _moments_to_beta(np.array([mu]), np.array([var]))
        return {
            "t_seconds": t,
            "family": "beta",
            "mean": mu,
            "variance": var,
            "std": float(math.sqrt(var)),
            "alpha": float(alpha_arr[0]),
            "beta": float(beta_arr[0]),
            "source": source,
            "time_model": "discrete_interp",
            "trained_horizons": list(trained),
            "bracket": {"lo": lo, "hi": hi, "weight": weight},
        }

    mu_lo, var_lo = _moments_at_horizon_level(values, lo)
    if exact:
        mu, var = mu_lo, var_lo
        source = f"level_h{lo:g}"
    else:
        mu_hi, var_hi = _moments_at_horizon_level(values, hi)
        mu = (1.0 - weight) * mu_lo + weight * mu_hi
        log_var = (1.0 - weight) * math.log(max(var_lo, 1e-8)) + weight * math.log(max(var_hi, 1e-8))
        var = math.exp(log_var)
        source = f"level_interp[{lo:g},{hi:g}]"
    mu = float(np.clip(mu, 1e-4, 1.0 - 1e-4))
    var = float(max(var, 0.005**2))
    return {
        "t_seconds": t,
        "family": "normal",
        "mean": mu,
        "variance": var,
        "std": float(math.sqrt(var)),
        "source": source,
        "time_model": "discrete_interp",
        "trained_horizons": list(trained),
        "bracket": {"lo": lo, "hi": hi, "weight": weight},
    }


def _beta_pdf(xs: np.ndarray, alpha: float, beta: float) -> np.ndarray:
    a = max(1e-3, float(alpha))
    b = max(1e-3, float(beta))
    log_norm = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    x = np.clip(xs, 1e-6, 1.0 - 1e-6)
    return np.exp((a - 1.0) * np.log(x) + (b - 1.0) * np.log1p(-x) - log_norm)


def _normal_pdf(xs: np.ndarray, mean: float, std: float) -> np.ndarray:
    if std <= 1e-9:
        out = np.zeros_like(xs, dtype=np.float64)
        idx = int(np.argmin(np.abs(xs - mean)))
        out[idx] = 1.0
        return out
    z = (xs - mean) / std
    return (1.0 / (std * np.sqrt(2.0 * np.pi))) * np.exp(-0.5 * z * z)


def future_up_price_pdf(
    t_seconds: float,
    features: pd.Series | np.ndarray | dict[str, Any],
    *,
    family: Family = "beta",
    grid: np.ndarray | None = None,
    current_up: float | None = None,
) -> dict[str, Any]:
    """Probability density of Up price t seconds later.

    Parameters
    ----------
    t_seconds:
        Continuous forecast horizon τ > 0. Prefers the continuous-time Beta
        model μ(X,t); falls back to discrete-horizon interpolation.
    features:
        Live feature row (FEATURE_COLUMNS), dict, or length-n feature vector.
    family:
        ``\"beta\"`` → Beta(α,β); ``\"level\"`` → Normal(μ,σ²).
    grid:
        Optional evaluation grid on (0,1). Defaults to a dense [0,1] mesh.
    current_up:
        Optional current Up mid; used for P(future > now).

    Returns
    -------
    dict with mean/variance/(alpha,beta), pdf points on [0,1], and P(rise).
    """
    moments = predict_moments(float(t_seconds), features, family=family)
    xs = (
        np.asarray(grid, dtype=np.float64)
        if grid is not None
        else np.linspace(1e-4, 1.0 - 1e-4, 201, dtype=np.float64)
    )
    if moments["family"] == "beta":
        dens = _beta_pdf(xs, float(moments["alpha"]), float(moments["beta"]))
    else:
        dens = _normal_pdf(xs, float(moments["mean"]), float(moments["std"]))

    trapz = getattr(np, "trapezoid", None) or np.trapz
    area = float(trapz(dens, xs)) if len(xs) > 1 else 1.0
    if area > 0:
        dens = dens / area

    current = 0.5 if current_up is None else float(np.clip(current_up, 0.0, 1.0))
    rise_mask = xs > current
    p_up = float(trapz(dens[rise_mask], xs[rise_mask]) / max(1e-12, trapz(dens, xs)))
    p_up = float(np.clip(p_up, 0.0, 1.0))

    out = {
        **moments,
        "horizon_seconds": float(t_seconds),
        "current_up": current,
        "probability_up": p_up,
        "probability_down": 1.0 - p_up,
        "direction": "UP" if p_up >= 0.5 else "DOWN",
        "confidence": abs(p_up - 0.5) * 2.0,
        "pdf": [{"x": float(x), "density": float(y)} for x, y in zip(xs, dens)],
    }
    return out


def future_up_price_pdfs(
    t_seconds: list[float] | tuple[float, ...],
    features: pd.Series | np.ndarray | dict[str, Any],
    *,
    family: Family = "beta",
    current_up: float | None = None,
) -> list[dict[str, Any]]:
    """Evaluate :func:`future_up_price_pdf` for several horizons."""
    return [
        future_up_price_pdf(float(t), features, family=family, current_up=current_up)
        for t in t_seconds
    ]
