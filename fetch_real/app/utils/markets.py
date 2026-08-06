from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import const


# Only 5-minute BTC up/down markets
_UPDOWN_5M_RE = re.compile(r"^btc-updown-5m-(?P<ts>\d+)$", re.IGNORECASE)


def is_updown_market(slug: str | None) -> bool:
    """True only for btc-updown-5m-{unix} markets (excludes 15m / 1h)."""
    text = (slug or "").strip().lower()
    if not text:
        return False
    if _UPDOWN_5M_RE.match(text):
        return True
    prefix = const.MARKET_SLUG_PREFIX.lower()
    return prefix in text and "-5m-" in text and "-15m-" not in text


def parse_updown_window(slug: str | None) -> tuple[datetime | None, datetime | None]:
    """btc-updown-5m-{unix_start} -> [start, start+5m)."""
    match = _UPDOWN_5M_RE.match((slug or "").strip())
    if not match:
        return None, None
    start_ts = int(match.group("ts"))
    start = datetime.fromtimestamp(start_ts, tz=UTC)
    end = start + timedelta(minutes=const.MARKET_DURATION_MINUTES)
    return start, end


def normalize_lookback_days(from_days: int, to_days: int = 0) -> tuple[int, int]:
    """Return (older_days, newer_days) with older >= newer >= 0."""
    older = max(0, int(from_days))
    newer = max(0, int(to_days))
    if newer > older:
        older, newer = newer, older
    return older, newer


def resolve_market_window(
    market: Any,
    *,
    lookback_days: int = const.HISTORY_LOOKBACK_DAYS,
    max_window: timedelta | None = None,
) -> tuple[datetime, datetime]:
    """Prefer slug-derived 5m window; never pull longer ranges."""
    if max_window is None:
        max_window = timedelta(minutes=const.MARKET_DURATION_MINUTES)
    slug = getattr(market, "slug", None) if not isinstance(market, dict) else market.get("slug")
    start = getattr(market, "start_time", None) if not isinstance(market, dict) else market.get("start_time")
    end = getattr(market, "end_time", None) if not isinstance(market, dict) else market.get("end_time")
    settlement = (
        getattr(market, "settlement_time", None)
        if not isinstance(market, dict)
        else market.get("settlement_time")
    )

    slug_start, slug_end = parse_updown_window(str(slug) if slug else None)
    if slug_start and slug_end:
        return slug_start, slug_end

    if start is None and end is None:
        end_dt = datetime.now(UTC)
        return end_dt - max_window, end_dt

    end_dt = end or settlement or datetime.now(UTC)
    start_dt = start or (end_dt - max_window)
    if end_dt - start_dt > max_window:
        start_dt = end_dt - max_window
    return start_dt, end_dt


def filter_updown_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        slug = str(row.get("slug") or "")
        if not is_updown_market(slug):
            continue
        start, end = parse_updown_window(slug)
        if start and not row.get("start_time"):
            row = {**row, "start_time": start}
        if end and not row.get("end_time"):
            row = {**row, "end_time": end, "settlement_time": row.get("settlement_time") or end}
        out.append(row)
    return out


def iter_5m_slugs(
    lookback_from_days: int,
    lookback_to_days: int = 0,
    *,
    end: datetime | None = None,
) -> list[str]:
    """
    Every btc-updown-5m-{unix} slug in [now - from_days, now - to_days).

    Examples:
      iter_5m_slugs(7)       -> last 7 days
      iter_5m_slugs(7, 3)    -> from 7 days ago until 3 days ago
    """
    from_days, to_days = normalize_lookback_days(lookback_from_days, lookback_to_days)
    end_dt = end or datetime.now(UTC)
    range_end = end_dt - timedelta(days=to_days)
    range_start = end_dt - timedelta(days=from_days)
    start_ts = int(range_start.timestamp())
    start_ts -= start_ts % const.MARKET_SLOT_SECONDS
    end_ts = int(range_end.timestamp())
    end_ts -= end_ts % const.MARKET_SLOT_SECONDS
    if end_ts <= start_ts:
        return []
    return [
        f"btc-updown-5m-{ts}"
        for ts in range(start_ts, end_ts, const.MARKET_SLOT_SECONDS)
    ]


def market_date_key(start_time: Any = None, slug: str | None = None) -> str:
    """UTC calendar date YYYY-MM-DD for folder layout."""
    if start_time is not None:
        if isinstance(start_time, datetime):
            dt = start_time
        elif isinstance(start_time, (int, float)):
            ts = int(start_time)
            if ts < 10_000_000_000:
                ts *= 1000
            dt = datetime.fromtimestamp(ts / 1000.0, tz=UTC)
        else:
            try:
                dt = datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
            except ValueError:
                dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).strftime("%Y-%m-%d")
    slug_start, _ = parse_updown_window(slug)
    if slug_start is not None:
        return slug_start.strftime("%Y-%m-%d")
    return datetime.now(UTC).strftime("%Y-%m-%d")
