from collections import deque
from datetime import datetime, timedelta

import numpy as np


class VolatilityTracker:
    def __init__(self, window_seconds: int = 60) -> None:
        self.window_seconds = window_seconds
        self._points: deque[tuple[datetime, float]] = deque(maxlen=5000)

    def update(self, ts: datetime, mid: float) -> dict[str, float | None]:
        self._points.append((ts, mid))
        cutoff = ts - timedelta(seconds=self.window_seconds)
        prices = [p for t, p in self._points if t >= cutoff]
        if len(prices) < 3:
            return {"std": None, "realized_vol": None, "atr": None}

        arr = np.asarray(prices, dtype=float)
        rets = np.diff(arr) / arr[:-1]
        std = float(np.std(rets)) if len(rets) else None
        realized = float(np.sqrt(np.sum(rets**2))) if len(rets) else None
        atr = float(np.mean(np.abs(np.diff(arr)))) if len(arr) > 1 else None
        return {"std": std, "realized_vol": realized, "atr": atr}

    def primary(self, ts: datetime, mid: float) -> float | None:
        return self.update(ts, mid).get("realized_vol")
