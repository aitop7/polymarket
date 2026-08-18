"""Feature relevance vs delta up_mid (wraps app.ml.feature_relevance).

  cd poly-monitor/backend
  python -m app.ml.feature_relevance --horizons 3,5
"""

from __future__ import annotations

from app.ml.feature_relevance import analyze, main

__all__ = ["analyze", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
