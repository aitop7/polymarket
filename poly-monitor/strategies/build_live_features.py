"""Build live prediction features → parquet (not a trading strategy).

  cd poly-monitor/backend
  python -m app.ml.build_live_features
  python -m app.ml.build_live_features --split chronological --max-markets 500

Writes fetch_real/features_live/{market_id}.parquet (+ manifest.json).
"""

from __future__ import annotations

from app.ml.build_live_features import build_live_features, main

__all__ = ["build_live_features", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
