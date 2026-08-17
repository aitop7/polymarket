"""Predict Up direction or mid T seconds ahead — train / evaluate entry point.

This file is intentionally not a trading strategy. It wraps the ML trainer:

  cd poly-monitor/backend
  python -m app.ml.train_predict_up --train --task direction --horizons 3,5
  python -m app.ml.train_predict_up --evaluate --task direction --horizons 3,5

Artifacts land in fetch_real/models/:
  predict_up_direction_h3.txt … predict_up_direction_h5.txt
  predict_up_direction_h*_metrics.json
  predict_up_direction_h*_eval.json
"""

from __future__ import annotations

from app.ml.train_predict_up import (
    evaluate_predict_up,
    evaluate_predict_up_direction,
    evaluate_predict_up_horizons,
    main,
    train_predict_up,
    train_predict_up_direction_horizon,
    train_predict_up_horizon,
)

__all__ = [
    "evaluate_predict_up",
    "evaluate_predict_up_direction",
    "evaluate_predict_up_horizons",
    "train_predict_up",
    "train_predict_up_direction_horizon",
    "train_predict_up_horizon",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
