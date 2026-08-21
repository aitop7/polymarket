"""BTC Up/Down market series (5m + 15m)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SeriesKey = Literal["5m", "15m"]


@dataclass(frozen=True, slots=True)
class MarketSeries:
    key: SeriesKey
    slug_prefix: str
    duration_s: int

    @property
    def duration_ms(self) -> int:
        return self.duration_s * 1000

    @property
    def slots_per_day(self) -> int:
        return 86_400 // self.duration_s

    def slug_for_start(self, start_unix: int) -> str:
        return f"{self.slug_prefix}-{int(start_unix)}"


SERIES_5M = MarketSeries(key="5m", slug_prefix="btc-updown-5m", duration_s=300)
SERIES_15M = MarketSeries(key="15m", slug_prefix="btc-updown-15m", duration_s=900)

ALL_SERIES: tuple[MarketSeries, ...] = (SERIES_5M, SERIES_15M)

_SERIES_BY_KEY: dict[str, MarketSeries] = {s.key: s for s in ALL_SERIES}
_SERIES_BY_PREFIX: dict[str, MarketSeries] = {
    s.slug_prefix.lower(): s for s in ALL_SERIES
}


def get_series(key: str | None) -> MarketSeries:
    if not key:
        return SERIES_5M
    hit = _SERIES_BY_KEY.get(str(key).strip().lower())
    return hit or SERIES_5M


def series_from_slug(slug: str | None) -> MarketSeries | None:
    text = (slug or "").strip().lower()
    if not text:
        return None
    for prefix, series in _SERIES_BY_PREFIX.items():
        if text.startswith(prefix + "-"):
            return series
    return None
