"""Evaluate LightGBM: prediction quality + threshold trading PnL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import lightgbm as lgb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from sklearn.calibration import calibration_curve
from sklearn.metrics import log_loss, roc_auc_score

from app.ml.load_features import feature_matrix, load_split
from app.utils.logger import logger, setup_logger

DEFAULT_THRESHOLDS = (0.02, 0.05, 0.10)


def _pred_metrics(y_true: np.ndarray, y_prob: np.ndarray) -> dict[str, float]:
    p = np.clip(y_prob, 1e-7, 1 - 1e-7)
    out: dict[str, float] = {"logloss": float(log_loss(y_true, p)), "n": int(len(y_true))}
    if len(np.unique(y_true)) > 1:
        out["auc"] = float(roc_auc_score(y_true, p))
    else:
        out["auc"] = float("nan")
    return out


def _max_drawdown(cum_pnl: np.ndarray) -> float:
    if len(cum_pnl) == 0:
        return 0.0
    peak = np.maximum.accumulate(cum_pnl)
    dd = cum_pnl - peak
    return float(dd.min()) if len(dd) else 0.0


def simulate_buy_up(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    up_price: np.ndarray,
    threshold: float,
) -> dict[str, float]:
    """
    If P(UP) - up_price > threshold: BUY UP at up_price.
    Settlement: +1 if winner else 0; PnL = settle - up_price per share.
    """
    edge = y_prob - up_price
    mask = edge > threshold
    n_trades = int(mask.sum())
    if n_trades == 0:
        return {
            "threshold": float(threshold),
            "n_trades": 0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "win_rate": float("nan"),
            "max_drawdown": 0.0,
            "sharpe": float("nan"),
        }

    settle = y_true[mask].astype(np.float64)
    prices = up_price[mask].astype(np.float64)
    pnl = settle - prices
    cum = np.cumsum(pnl)
    wins = settle >= 0.999  # winner==1
    # Sharpe on per-trade PnL (assume unit stakes)
    std = float(pnl.std(ddof=1)) if n_trades > 1 else 0.0
    sharpe = float(pnl.mean() / std * np.sqrt(n_trades)) if std > 1e-12 else float("nan")

    return {
        "threshold": float(threshold),
        "n_trades": n_trades,
        "total_pnl": float(pnl.sum()),
        "avg_pnl": float(pnl.mean()),
        "win_rate": float(wins.mean()),
        "max_drawdown": _max_drawdown(cum),
        "sharpe": sharpe,
    }


def save_calibration_plot(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    path: Path,
    *,
    n_bins: int = 10,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frac_pos, mean_pred = calibration_curve(y_true, y_prob, n_bins=n_bins, strategy="quantile")
    fig, ax = plt.subplots(figsize=(6, 6))
    ax.plot([0, 1], [0, 1], linestyle="--", color="gray", label="perfect")
    ax.plot(mean_pred, frac_pos, marker="o", label="model")
    ax.set_xlabel("Mean predicted P(UP)")
    ax.set_ylabel("Fraction UP (winner=1)")
    ax.set_title("Calibration (test)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def evaluate(
    features_root: Path,
    models_dir: Path,
    *,
    thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS,
    max_markets: int | None = None,
) -> dict[str, Any]:
    features_root = Path(features_root)
    models_dir = Path(models_dir)
    model_path = models_dir / "lgbm_baseline.txt"
    if not model_path.is_file():
        raise FileNotFoundError(f"Missing model: {model_path}")

    model = lgb.Booster(model_file=str(model_path))

    val_df = load_split(features_root, "validation", max_markets=max_markets)
    test_df = load_split(features_root, "test", max_markets=max_markets)

    X_val, y_val, meta_val = feature_matrix(val_df)
    X_test, y_test, meta_test = feature_matrix(test_df)

    p_val = model.predict(X_val)
    p_test = model.predict(X_test)

    quality = {
        "validation": _pred_metrics(y_val, p_val),
        "test": _pred_metrics(y_test, p_test),
    }

    cal_path = models_dir / "calibration_test.png"
    save_calibration_plot(y_test, p_test, cal_path)

    up_val = meta_val["up_price"].to_numpy(dtype=np.float64)
    up_test = meta_test["up_price"].to_numpy(dtype=np.float64)

    val_sims = [simulate_buy_up(y_val, p_val, up_val, t) for t in thresholds]
    # pick threshold with best total_pnl on validation (prefer more trades if tie)
    best = max(val_sims, key=lambda s: (s["total_pnl"], s["n_trades"]))
    chosen = float(best["threshold"])
    test_sim = simulate_buy_up(y_test, p_test, up_test, chosen)

    report: dict[str, Any] = {
        "prediction_quality": quality,
        "calibration_plot": str(cal_path.resolve()),
        "threshold_sweep_validation": val_sims,
        "chosen_threshold": chosen,
        "trading_test": test_sim,
    }

    out_path = models_dir / "eval_report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info("Wrote {}", out_path)
    logger.info(
        "test logloss={:.5f} auc={:.5f} | threshold={} test_pnl={:.4f} trades={} win_rate={}",
        quality["test"]["logloss"],
        quality["test"]["auc"],
        chosen,
        test_sim["total_pnl"],
        test_sim["n_trades"],
        test_sim["win_rate"],
    )
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Evaluate LightGBM baseline + threshold trading")
    parser.add_argument("--features", type=Path, default=Path("features"))
    parser.add_argument("--models-dir", type=Path, default=Path("models"))
    parser.add_argument("--max-markets", type=int, default=None)
    parser.add_argument(
        "--thresholds",
        type=float,
        nargs="+",
        default=list(DEFAULT_THRESHOLDS),
    )
    args = parser.parse_args(argv)

    setup_logger()
    features_root = args.features if args.features.is_absolute() else Path.cwd() / args.features
    models_dir = args.models_dir if args.models_dir.is_absolute() else Path.cwd() / args.models_dir

    report = evaluate(
        features_root,
        models_dir,
        thresholds=tuple(args.thresholds),
        max_markets=args.max_markets,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
