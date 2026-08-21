"""BTC Up/Down market series (5m + 15m) — shared by live + history."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

SeriesKey = Literal["5m", "15m"]

_UPDOWN_SLUG_RE = re.compile(r"(?i)^btc-updown-(5m|15m)-(\d+)$")


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

    @property
    def slug_re(self) -> re.Pattern[str]:
        return re.compile(rf"(?i)^{re.escape(self.slug_prefix)}-(\d+)$")


SERIES_5M = MarketSeries(key="5m", slug_prefix="btc-updown-5m", duration_s=300)
SERIES_15M = MarketSeries(key="15m", slug_prefix="btc-updown-15m", duration_s=900)

ALL_SERIES: tuple[MarketSeries, ...] = (SERIES_5M, SERIES_15M)

_SERIES_BY_KEY: dict[str, MarketSeries] = {s.key: s for s in ALL_SERIES}


def get_series(key: str | None = None) -> MarketSeries:
    if not key:
        return SERIES_5M
    hit = _SERIES_BY_KEY.get(str(key).strip().lower())
    return hit or SERIES_5M


def series_from_slug(slug: str | None) -> MarketSeries | None:
    text = (slug or "").strip()
    m = _UPDOWN_SLUG_RE.match(text)
    if not m:
        return None
    return get_series(m.group(1))


def series_key_from_meta(meta: dict[str, Any] | None) -> SeriesKey:
    if not meta:
        return "5m"
    raw = meta.get("series")
    if raw in ("5m", "15m"):
        return raw  # type: ignore[return-value]
    hit = series_from_slug(str(meta.get("slug") or ""))
    return hit.key if hit else "5m"


def row_matches_series(row: dict[str, Any], series: MarketSeries | str) -> bool:
    s = series if isinstance(series, MarketSeries) else get_series(series)
    key = row.get("series")
    if key in ("5m", "15m"):
        return key == s.key
    slug = str(row.get("slug") or "")
    hit = series_from_slug(slug)
    if hit is not None:
        return hit.key == s.key
    # Legacy TWAP rows without slug: treat as 5m only.
    return s.key == "5m"


def filter_rows_by_series(
    rows: list[dict[str, Any]], series: MarketSeries | str | None
) -> list[dict[str, Any]]:
    if series is None:
        return rows
    s = series if isinstance(series, MarketSeries) else get_series(series)
    return [r for r in rows if row_matches_series(r, s)]
