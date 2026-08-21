"""Gamma discovery for btc-updown-5m / btc-updown-15m markets."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from app.config import settings
from app.series import ALL_SERIES, MarketSeries, SERIES_5M, series_from_slug

_UPDOWN_SLUG_RE = re.compile(r"(?i)^btc-updown-(5m|15m)-(\d+)$")


def window_start_unix(
    now_s: float | None = None, *, duration_s: int | None = None
) -> int:
    ts = int(now_s if now_s is not None else time.time())
    dur = int(duration_s if duration_s is not None else settings.market_duration_s)
    if dur <= 0:
        dur = 300
    return ts - (ts % dur)


def parse_token_ids(market: dict[str, Any]) -> tuple[str | None, str | None]:
    raw = market.get("clobTokenIds") or market.get("clob_token_ids")
    if raw is None:
        return None, None
    if isinstance(raw, str):
        try:
            ids = json.loads(raw)
        except json.JSONDecodeError:
            ids = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        ids = list(raw)
    yes = str(ids[0]) if len(ids) > 0 else None
    no = str(ids[1]) if len(ids) > 1 else None
    return yes, no


def parse_window(
    market: dict[str, Any], *, series: MarketSeries | None = None
) -> tuple[int, int]:
    slug = str(market.get("slug") or "")
    match = _UPDOWN_SLUG_RE.match(slug)
    if match:
        start_s = int(match.group(2))
        series = series or series_from_slug(slug) or SERIES_5M
    else:
        series = series or SERIES_5M
        start_s = window_start_unix(duration_s=series.duration_s)
    end_s = start_s + series.duration_s
    return start_s * 1000, end_s * 1000


class Discovery:
    def __init__(self, series: MarketSeries | None = None) -> None:
        self.series = series or SERIES_5M
        self._http = httpx.AsyncClient(
            base_url=settings.gamma_url,
            timeout=httpx.Timeout(8.0, connect=4.0),
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def get_market_by_slug(self, slug: str) -> dict[str, Any] | None:
        try:
            resp = await self._http.get("/events", params={"slug": slug})
            resp.raise_for_status()
            events = resp.json()
            if isinstance(events, list) and events:
                markets = events[0].get("markets") or []
                for market in markets:
                    if str(market.get("slug") or "") == slug or len(markets) == 1:
                        return market
                if markets:
                    return markets[0]
        except Exception:
            return None
        return None

    async def discover_active(self) -> dict[str, Any] | None:
        series = self.series
        start = window_start_unix(duration_s=series.duration_s)
        dur = series.duration_s
        for offset in (0, -dur, dur, -2 * dur):
            slug = series.slug_for_start(start + offset)
            market = await self.get_market_by_slug(slug)
            if not market:
                continue
            if bool(market.get("closed")):
                continue
            return market
        return None

    async def get_market_by_id(self, market_id: str) -> dict[str, Any] | None:
        try:
            resp = await self._http.get("/markets", params={"id": market_id})
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict):
                return data
        except Exception:
            return None
        return None


def default_discoveries() -> list[Discovery]:
    return [Discovery(series) for series in ALL_SERIES]
