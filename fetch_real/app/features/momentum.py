from collections import deque
from datetime import datetime, timedelta


class MomentumTracker:
    """Rolling mid-price returns over 1s / 5s / 30s / 60s windows."""

    def __init__(self) -> None:
        self._points: deque[tuple[datetime, float]] = deque(maxlen=5000)

    def update(self, ts: datetime, mid: float) -> dict[str, float | None]:
        self._points.append((ts, mid))
        return {
            "mom_1s": self._return(ts, 1),
            "mom_5s": self._return(ts, 5),
            "mom_30s": self._return(ts, 30),
            "mom_60s": self._return(ts, 60),
        }

    def _return(self, now: datetime, seconds: int) -> float | None:
        cutoff = now - timedelta(seconds=seconds)
        older = None
        for ts, price in self._points:
            if ts <= cutoff:
                older = price
            else:
                break
        if older is None or older == 0:
            # fallback: earliest available in window
            if not self._points:
                return None
            first_ts, first_price = self._points[0]
            if first_ts >= now or first_price == 0:
                return None
            older = first_price
        latest = self._points[-1][1]
        return (latest - older) / older

    def primary(self, ts: datetime, mid: float) -> float | None:
        moms = self.update(ts, mid)
        return moms.get("mom_5s")
