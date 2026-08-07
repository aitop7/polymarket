"""Train a LightGBM baseline: P(UP) = P(winner=1)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
from sklearn.metrics import log_loss, roc_auc_score

from app.dataset.feature_schema import FEATURE_COLUMNS, SCHEMA_VERSION
from app.ml.load_features import feature_matrix, load_split
from app.utils.logger import logger, setup_logger


DEFAULT_PARAMS: dict[str, Any] = {
    "objective": "binary",
    "metric": "binary_logloss",
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


def _metrics(y_true: np.ndarray, y_prob: np.ndarray) -> dict[str, float]:
    # clip for log_loss stability
    p = np.clip(y_prob, 1e-7, 1 - 1e-7)
    out: dict[str, float] = {"logloss": float(log_loss(y_true, p))}
    if len(np.unique(y_true)) > 1:
        out["auc"] = float(roc_auc_score(y_true, p))
    else:
        out["auc"] = float("nan")
    return out


def train(
    features_root: Path,
    models_dir: Path,
    *,
    num_boost_round: int = 500,
    early_stopping_rounds: int = 50,
    max_markets: int | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    features_root = Path(features_root)
    models_dir = Path(models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Loading train features from {}", features_root / "train")
    train_df = load_split(features_root, "train", max_markets=max_markets)
    logger.info("Loading validation features")
    val_df = load_split(features_root, "validation", max_markets=max_markets)
    logger.info("Loading test features")
    test_df = load_split(features_root, "test", max_markets=max_markets)

    X_train, y_train, _ = feature_matrix(train_df)
    X_val, y_val, _ = feature_matrix(val_df)
    X_test, y_test, _ = feature_matrix(test_df)

    logger.info(
        "shapes train={} val={} test={} features={}",
        X_train.shape,
        X_val.shape,
        X_test.shape,
        len(FEATURE_COLUMNS),
    )

    boost_params = {**DEFAULT_PARAMS, **(params or {})}
    dtrain = lgb.Dataset(X_train, label=y_train, feature_name=list(FEATURE_COLUMNS), free_raw_data=False)
    dval = lgb.Dataset(X_val, label=y_val, reference=dtrain, feature_name=list(FEATURE_COLUMNS), free_raw_data=False)

    callbacks = [
        lgb.early_stopping(early_stopping_rounds, verbose=True),
        lgb.log_evaluation(period=50),
    ]
    model = lgb.train(
        boost_params,
        dtrain,
        num_boost_round=num_boost_round,
        valid_sets=[dtrain, dval],
        valid_names=["train", "valid"],
        callbacks=callbacks,
    )

    model_path = models_dir / "lgbm_baseline.txt"
    model.save_model(str(model_path))

    feature_names_path = models_dir / "feature_names.json"
    feature_names_path.write_text(json.dumps(list(FEATURE_COLUMNS), indent=2), encoding="utf-8")

    preds = {
        "train": model.predict(X_train, num_iteration=model.best_iteration),
        "validation": model.predict(X_val, num_iteration=model.best_iteration),
        "test": model.predict(X_test, num_iteration=model.best_iteration),
    }
    metrics = {
        "schema_version": SCHEMA_VERSION,
        "best_iteration": int(model.best_iteration or 0),
        "n_features": len(FEATURE_COLUMNS),
        "n_rows": {
            "train": int(len(y_train)),
            "validation": int(len(y_val)),
            "test": int(len(y_test)),
        },
        "train": _metrics(y_train, preds["train"]),
        "validation": _metrics(y_val, preds["validation"]),
        "test": _metrics(y_test, preds["test"]),
        "params": boost_params,
    }

    importance = sorted(
        zip(FEATURE_COLUMNS, model.feature_importance(importance_type="gain").tolist()),
        key=lambda x: x[1],
        reverse=True,
    )
    metrics["feature_importance_gain_top20"] = [
        {"feature": name, "gain": float(gain)} for name, gain in importance[:20]
    ]

    metrics_path = models_dir / "metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    logger.info("Saved model -> {}", model_path)
    logger.info(
        "metrics train logloss={:.5f} auc={:.5f} | valid logloss={:.5f} auc={:.5f} | test logloss={:.5f} auc={:.5f}",
        metrics["train"]["logloss"],
        metrics["train"]["auc"],
        metrics["validation"]["logloss"],
        metrics["validation"]["auc"],
        metrics["test"]["logloss"],
        metrics["test"]["auc"],
    )
    logger.info("Top features:")
    for item in metrics["feature_importance_gain_top20"][:10]:
        logger.info("  {} {:.1f}", item["feature"], item["gain"])

    return metrics


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Train LightGBM baseline on Step-2 features")
    parser.add_argument("--features", type=Path, default=Path("features"))
    parser.add_argument("--models-dir", type=Path, default=Path("models"))
    parser.add_argument("--num-boost-round", type=int, default=500)
    parser.add_argument("--early-stopping", type=int, default=50)
    parser.add_argument("--max-markets", type=int, default=None, help="Debug: limit markets per split")
    args = parser.parse_args(argv)

    setup_logger()
    features_root = args.features if args.features.is_absolute() else Path.cwd() / args.features
    models_dir = args.models_dir if args.models_dir.is_absolute() else Path.cwd() / args.models_dir

    metrics = train(
        features_root,
        models_dir,
        num_boost_round=args.num_boost_round,
        early_stopping_rounds=args.early_stopping,
        max_markets=args.max_markets,
    )
    print(json.dumps({k: metrics[k] for k in ("best_iteration", "train", "validation", "test")}, indent=2))


if __name__ == "__main__":
    main()
