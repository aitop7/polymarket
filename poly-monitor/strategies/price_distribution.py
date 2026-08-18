"""Continuous-time Up-price density.

Train:

  cd poly-monitor/backend
  python -m app.ml.train_predict_up_beta_ct --train --t-min 0.5 --t-max 30

Infer for any t > 0:

  from app.ml.price_distribution import future_up_price_pdf
  pdf = future_up_price_pdf(t_seconds=7.25, features=row, family=\"beta\")
"""

from __future__ import annotations

from app.ml.price_distribution import (
    available_pdf_horizons,
    future_up_price_pdf,
    future_up_price_pdfs,
    predict_moments,
)
from app.ml.train_predict_up_beta import evaluate_beta_horizon, train_beta_horizon
from app.ml.train_predict_up_beta_ct import (
    continuous_model_ready,
    evaluate_beta_continuous,
    train_beta_continuous,
)

__all__ = [
    "available_pdf_horizons",
    "continuous_model_ready",
    "evaluate_beta_continuous",
    "evaluate_beta_horizon",
    "future_up_price_pdf",
    "future_up_price_pdfs",
    "predict_moments",
    "train_beta_continuous",
    "train_beta_horizon",
]
