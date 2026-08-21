"""Up/Down market series (BTC 5m/15m + BNB 15m) — shared by live + history."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

SeriesKey = Literal["5m", "15m", "bnb-15m"]
AssetKey = Literal["BTC", "BNB"]

# btc-updown-5m|15m-{ts} or bnb-updown-15m-{ts}
_UPDOWN_SLUG_RE = re.compile(
    r"(?i)^(btc|bnb)-updown-(5m|15m)-(\d+)$"
)

_SERIES_PATTERN = r"^(5m|15m|bnb-15m)$"


@dataclass(frozen=True, slots=True)
class MarketSeries:
    key: SeriesKey
    slug_prefix: str
    duration_s: int
    asset: AssetKey = "BTC"
    binance_symbol: str = "BTCUSDT"
    chainlink_symbol: str = "BTCUSD"
    rtds_symbol: str = "btc/usd"

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

    @property
    def scope(self) -> str:
        """Wallet / activity scope id, e.g. btc_updown_5m, bnb_updown_15m."""
        asset = self.asset.lower()
        if self.key == "bnb-15m":
            return f"{asset}_updown_15m"
        return f"{asset}_updown_{self.key}"


SERIES_5M = MarketSeries(
    key="5m",
    slug_prefix="btc-updown-5m",
    duration_s=300,
    asset="BTC",
    binance_symbol="BTCUSDT",
    chainlink_symbol="BTCUSD",
    rtds_symbol="btc/usd",
)
SERIES_15M = MarketSeries(
    key="15m",
    slug_prefix="btc-updown-15m",
    duration_s=900,
    asset="BTC",
    binance_symbol="BTCUSDT",
    chainlink_symbol="BTCUSD",
    rtds_symbol="btc/usd",
)
SERIES_BNB_15M = MarketSeries(
    key="bnb-15m",
    slug_prefix="bnb-updown-15m",
    duration_s=900,
    asset="BNB",
    binance_symbol="BNBUSDT",
    chainlink_symbol="BNBUSD",
    rtds_symbol="bnb/usd",
)

ALL_SERIES: tuple[MarketSeries, ...] = (SERIES_5M, SERIES_15M, SERIES_BNB_15M)

_SERIES_BY_KEY: dict[str, MarketSeries] = {s.key: s for s in ALL_SERIES}


def series_query_pattern() -> str:
    """FastAPI Query/Field regex for allowed series keys."""
    return _SERIES_PATTERN


def get_series(key: str | None = None) -> MarketSeries:
    if not key:
        return SERIES_5M
    hit = _SERIES_BY_KEY.get(str(key).strip().lower())
    return hit or SERIES_5M


def _key_from_asset_tf(asset: str, tf: str) -> SeriesKey | None:
    a = asset.lower()
    t = tf.lower()
    if a == "btc" and t == "5m":
        return "5m"
    if a == "btc" and t == "15m":
        return "15m"
    if a == "bnb" and t == "15m":
        return "bnb-15m"
    return None


def series_from_slug(slug: str | None) -> MarketSeries | None:
    text = (slug or "").strip()
    m = _UPDOWN_SLUG_RE.match(text)
    if not m:
        return None
    key = _key_from_asset_tf(m.group(1), m.group(2))
    return get_series(key) if key else None


def series_key_from_meta(meta: dict[str, Any] | None) -> SeriesKey:
    if not meta:
        return "5m"
    raw = str(meta.get("series") or "").strip().lower()
    if raw in _SERIES_BY_KEY:
        return raw  # type: ignore[return-value]
    hit = series_from_slug(str(meta.get("slug") or ""))
    return hit.key if hit else "5m"


def row_matches_series(row: dict[str, Any], series: MarketSeries | str) -> bool:
    s = series if isinstance(series, MarketSeries) else get_series(series)
    key = str(row.get("series") or "").strip().lower()
    if key in _SERIES_BY_KEY:
        return key == s.key
    slug = str(row.get("slug") or "")
    hit = series_from_slug(slug)
    if hit is not None:
        return hit.key == s.key
    # Legacy TWAP rows without slug: treat as BTC 5m only.
    return s.key == "5m"


def filter_rows_by_series(
    rows: list[dict[str, Any]], series: MarketSeries | str | None
) -> list[dict[str, Any]]:
    if series is None:
        return rows
    s = series if isinstance(series, MarketSeries) else get_series(series)
    return [r for r in rows if row_matches_series(r, s)]
