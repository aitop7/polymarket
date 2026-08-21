"""Cross-market wallet profile / activity via Polymarket public APIs."""

from __future__ import annotations

import asyncio
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Iterator
from zoneinfo import ZoneInfo

import httpx

DATA_API_URL = "https://data-api.polymarket.com"
PNL_API_URL = "https://user-pnl-api.polymarket.com"
LB_API_URL = "https://lb-api.polymarket.com"

# Parallel page fetches against Polymarket data-api (closed-positions / activity).
_PAGE_CONCURRENCY = 8
_CLOSED_PAGE = 50
_ACTIVITY_PAGE = 500


def _http_client(*, timeout_s: float = 90.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s, connect=8.0),
        limits=httpx.Limits(max_connections=48, max_keepalive_connections=24),
        follow_redirects=True,
    )

from app.core.series import get_series, series_from_slug

_ET = ZoneInfo("America/New_York")
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_UPDOWN_SLUG_RE = re.compile(r"^(btc|bnb)-updown-(5m|15m)-(\d+)$", re.I)
# Backward-compatible aliases used by older call sites / tests.
_BTC_UPDOWN_SLUG_RE = re.compile(r"^btc-updown-(5m|15m)-(\d+)$", re.I)
_BTC_UPDOWN_5M_SLUG_RE = re.compile(r"^btc-updown-5m-\d+$", re.I)
_BTC_UPDOWN_TITLE_RE = re.compile(r"^bitcoin\s+up\s+or\s+down\b", re.I)
_BNB_UPDOWN_TITLE_RE = re.compile(r"^bnb\s+up\s+or\s+down\b", re.I)
_BTC_UPDOWN_5M_TITLE_RE = _BTC_UPDOWN_TITLE_RE

_active_series: ContextVar[str] = ContextVar("wallet_market_series", default="5m")
_VALID_SERIES = frozenset({"5m", "15m", "bnb-15m"})


def normalize_series(series: str | None) -> str:
    key = (series or "5m").strip().lower()
    return key if key in _VALID_SERIES else "5m"


def series_scope(series: str | None = None) -> str:
    return get_series(
        normalize_series(series if series is not None else _active_series.get())
    ).scope


@contextmanager
def use_btc_series(series: str | None) -> Iterator[str]:
    key = normalize_series(series)
    token = _active_series.set(key)
    try:
        yield key
    finally:
        _active_series.reset(token)


def slug_series(slug: str | None) -> str | None:
    hit = series_from_slug(str(slug or "").strip())
    return hit.key if hit else None


def slug_duration_s(slug: str | None) -> int | None:
    hit = series_from_slug(str(slug or "").strip())
    return hit.duration_s if hit else None

_PNL_INTERVALS = {
    # Matches Polymarket profile chart (3ur9websez40a.js):
    # fidelity: 1D→1h, 1W→3h, 1M→18h, else→1d
    "1d": ("1d", "1h"),
    "1w": ("1w", "3h"),
    "1m": ("1m", "18h"),
    "1y": ("all", "1d"),
    "ytd": ("all", "1d"),
    "all": ("all", "1d"),
    "max": ("max", "1d"),
}

# Rolling window cutoffs (ms), same constants Polymarket uses.
_PNL_WINDOW_MS = {
    "1d": 86_400_000,       # 864e5
    "1w": 604_800_000,      # 6048e5
    "1m": 2_592_000_000,    # 2592e6 ≈ 30d
    "1y": 31_536_000_000,   # 31536e6
}


def _window_cutoff_ms(interval: str, now_ms: int | None = None) -> int | None:
    now = int(now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    key = interval.lower()
    if key == "ytd":
        # Local calendar YTD like Polymarket: new Date(year, 0, 1)
        local = datetime.now().astimezone()
        start = datetime(local.year, 1, 1, tzinfo=local.tzinfo)
        return int(start.timestamp() * 1000)
    if key in {"all", "max"}:
        return None
    ms = _PNL_WINDOW_MS.get(key)
    if ms is None:
        return None
    return now - ms


def _account_age_days(points: list[dict[str, Any]], now_ms: int | None = None) -> float | None:
    if not points:
        return None
    now = int(now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    first_t = int(points[0]["t"])
    return max(0.0, (now - first_t) / 86_400_000.0)


def _period_too_short(interval: str, age_days: float | None) -> bool:
    """Polymarket shows absolute level (not delta) when account is younger than the period."""
    if age_days is None:
        return False
    key = interval.lower()
    if key == "1d":
        return age_days < 1
    if key == "1w":
        return age_days < 7
    if key == "1m":
        return age_days < 31
    if key == "1y":
        return age_days < 365
    if key == "ytd":
        local = datetime.now().astimezone()
        start = datetime(local.year, 1, 1, tzinfo=local.tzinfo)
        ytd_days = (datetime.now().astimezone() - start).total_seconds() / 86_400.0
        return age_days < ytd_days
    return False


def _round_pnl_to_cents(value: float) -> float:
    return round(float(value) * 100.0) / 100.0


def normalize_wallet(address: str) -> str:
    raw = (address or "").strip()
    if not _ADDR_RE.match(raw):
        raise ValueError("Invalid wallet address (expected 0x + 40 hex chars)")
    return raw.lower()


def is_btc_updown_market(
    *,
    slug: str | None = None,
    title: str | None = None,
    series: str | None = None,
) -> bool:
    """True for Up/Down markets in the active/requested series (BTC 5m/15m, BNB 15m)."""
    want = normalize_series(series if series is not None else _active_series.get())
    s = str(slug or "").strip()
    if s:
        return slug_series(s) == want
    t = str(title or "").strip()
    # Title-only rows lack a series marker; keep legacy title matching.
    if want == "5m":
        return bool(t and _BTC_UPDOWN_TITLE_RE.match(t))
    if want == "15m":
        return bool(t and _BTC_UPDOWN_TITLE_RE.match(t))
    if want == "bnb-15m":
        return bool(t and _BNB_UPDOWN_TITLE_RE.match(t))
    return False


def is_btc_updown_5m(*, slug: str | None = None, title: str | None = None) -> bool:
    """True for BTC Up/Down markets matching the active series context (default 5m)."""
    return is_btc_updown_market(slug=slug, title=title)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": "poly-monitor/1.0",
        "Accept": "application/json",
    }


def _ts_ms(v: Any) -> int | None:
    try:
        ts = int(v or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts < 10_000_000_000:
        ts *= 1000
    return ts


def _et_day_bounds(date_et: str) -> tuple[int, int]:
    """Return [start_ms, end_ms) for an ET calendar day (YYYY-MM-DD)."""
    day = datetime.strptime(date_et, "%Y-%m-%d").replace(tzinfo=_ET)
    start = day
    end = day + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _et_date_from_ms(ts_ms: int) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=_ET).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


_REBATE_TYPES = frozenset({"MAKER_REBATE", "TAKER_REBATE"})


def _norm_rebate(row: dict[str, Any]) -> dict[str, Any] | None:
    typ = str(row.get("type") or "").upper()
    if typ not in _REBATE_TYPES:
        return None
    ts = _ts_ms(row.get("timestamp"))
    if ts is None:
        return None
    try:
        usd = float(row.get("usdcSize") if row.get("usdcSize") is not None else row.get("size") or 0)
    except (TypeError, ValueError):
        usd = 0.0
    day = _et_date_from_ms(ts)
    if not day:
        return None
    tx = str(row.get("transactionHash") or row.get("transaction_hash") or "") or None
    return {
        "timestamp": ts,
        "date": day,
        "type": typ,
        "usd": usd,
        "transaction_hash": tx,
        "polygonscan_url": f"https://polygonscan.com/tx/{tx}" if tx else None,
    }


async def _paginate_rebate_activity(
    client: httpx.AsyncClient,
    wallet: str,
    *,
    max_rows: int = 5_000,
) -> list[dict[str, Any]]:
    """Account-level MAKER_REBATE / TAKER_REBATE rows (not market-scoped)."""
    page = 100
    offset = 0
    out: list[dict[str, Any]] = []
    while offset < max_rows:
        try:
            batch = await _get_json(
                client,
                f"{DATA_API_URL}/activity",
                params={
                    "user": wallet,
                    "limit": page,
                    "offset": offset,
                    "type": "MAKER_REBATE,TAKER_REBATE",
                },
            )
        except httpx.HTTPStatusError:
            break
        if not isinstance(batch, list) or not batch:
            break
        for raw in batch:
            item = _norm_rebate(raw if isinstance(raw, dict) else {})
            if item is not None:
                out.append(item)
        if len(batch) < page:
            break
        offset += len(batch)
        if len(out) >= max_rows:
            break
    return out[:max_rows]


def _rebate_rollups(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_day: dict[str, float] = {}
    maker = 0.0
    taker = 0.0
    for row in rows:
        day = str(row.get("date") or "")
        try:
            usd = float(row.get("usd") or 0.0)
        except (TypeError, ValueError):
            usd = 0.0
        if day:
            by_day[day] = float(by_day.get(day) or 0.0) + usd
        typ = str(row.get("type") or "").upper()
        if typ == "MAKER_REBATE":
            maker += usd
        elif typ == "TAKER_REBATE":
            taker += usd
    for day, usd in list(by_day.items()):
        by_day[day] = _round_pnl_to_cents(usd)
    total = _round_pnl_to_cents(maker + taker)
    return {
        "total_rebates": total,
        "maker_rebates": _round_pnl_to_cents(maker),
        "taker_rebates": _round_pnl_to_cents(taker),
        "rebates_by_day": by_day,
        "rebate_events": len(rows),
    }


async def fetch_wallet_rebates(address: str) -> dict[str, Any]:
    """Account-level maker/taker fee rebates from Polymarket activity."""
    wallet = normalize_wallet(address)
    async with _http_client(timeout_s=60.0) as client:
        rows = await _paginate_rebate_activity(client, wallet)
    roll = _rebate_rollups(rows)
    return {
        "wallet": wallet,
        "scope": "account",
        **roll,
        "rebates": rows,
    }


_TOTAL_PNL_INTERVALS = {
    # Orbscan-style account chart: 1D / 1W / 1M / ALL
    "1d": ("1d", "1h"),
    "1w": ("1w", "1h"),
    "1m": ("1m", "12h"),
    "all": ("all", "1d"),
    "max": ("max", "1d"),
}

_CASHFLOW_TYPES = (
    "DEPOSIT,WITHDRAWAL,MAKER_REBATE,TAKER_REBATE,REWARD,REFERRAL_REWARD,YIELD"
)


async def _paginate_cashflow_activity(
    client: httpx.AsyncClient,
    wallet: str,
    *,
    start_ms: int | None = None,
    max_rows: int = 8_000,
) -> list[dict[str, Any]]:
    """Account cashflow rows (deposits, withdrawals, rebates, rewards)."""
    page = 100
    offset = 0
    out: list[dict[str, Any]] = []
    start_s = max(0, int(start_ms) // 1000) if start_ms else None
    while offset < max_rows:
        params: dict[str, Any] = {
            "user": wallet,
            "limit": page,
            "offset": offset,
            "type": _CASHFLOW_TYPES,
            "excludeDepositsWithdrawals": "false",
        }
        if start_s is not None:
            params["start"] = start_s
        try:
            batch = await _get_json(client, f"{DATA_API_URL}/activity", params=params)
        except httpx.HTTPStatusError:
            break
        if not isinstance(batch, list) or not batch:
            break
        stop_old = False
        for raw in batch:
            if not isinstance(raw, dict):
                continue
            typ = str(raw.get("type") or "").upper()
            ts = _ts_ms(raw.get("timestamp"))
            if ts is None:
                continue
            if start_ms is not None and ts < start_ms - 86_400_000:
                stop_old = True
                break
            try:
                usd = float(
                    raw.get("usdcSize")
                    if raw.get("usdcSize") is not None
                    else raw.get("size")
                    or 0
                )
            except (TypeError, ValueError):
                usd = 0.0
            out.append({"timestamp": ts, "type": typ, "usd": usd})
        if stop_old or len(batch) < page:
            break
        offset += len(batch)
        if len(out) >= max_rows:
            break
    return out[:max_rows]


def _cashflow_bucket(typ: str) -> str | None:
    t = (typ or "").upper()
    if t == "DEPOSIT":
        return "deposit"
    if t == "WITHDRAWAL":
        return "withdraw"
    if t in {"MAKER_REBATE", "TAKER_REBATE", "REWARD", "REFERRAL_REWARD", "YIELD"}:
        return "reward"
    return None


async def fetch_wallet_total_pnl_chart(
    address: str, interval: str = "1w"
) -> dict[str, Any]:
    """Account-level Total PnL line + fee/reward/deposit/withdraw bars (Orbscan-style)."""
    wallet = normalize_wallet(address)
    key = (interval or "1w").strip().lower()
    if key not in _TOTAL_PNL_INTERVALS:
        raise ValueError(f"Invalid interval (use {', '.join(_TOTAL_PNL_INTERVALS)})")
    api_interval, fidelity = _TOTAL_PNL_INTERVALS[key]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cutoff = _window_cutoff_ms(key, now_ms=now_ms)

    async with _http_client(timeout_s=90.0) as client:
        pnl_task = _get_json(
            client,
            f"{PNL_API_URL}/user-pnl",
            params={
                "user_address": wallet,
                "interval": api_interval,
                "fidelity": fidelity,
            },
        )
        flows_task = _paginate_cashflow_activity(
            client, wallet, start_ms=cutoff, max_rows=8_000
        )
        raw_pnl, flows = await asyncio.gather(pnl_task, flows_task)

    points_raw: list[dict[str, Any]] = []
    if isinstance(raw_pnl, list):
        for row in raw_pnl:
            if not isinstance(row, dict):
                continue
            ts = _ts_ms(row.get("t"))
            if ts is None:
                continue
            try:
                p = float(row.get("p"))
            except (TypeError, ValueError):
                continue
            points_raw.append({"t": ts, "pnl_abs": p})
    points_raw.sort(key=lambda r: int(r["t"]))

    if cutoff is not None:
        pre = [p for p in points_raw if p["t"] < cutoff]
        in_win = [p for p in points_raw if p["t"] >= cutoff]
        if pre and in_win:
            points_raw = [pre[-1], *in_win]
        else:
            points_raw = in_win or points_raw

    base = float(points_raw[0]["pnl_abs"]) if points_raw else 0.0
    # Relative series (matches Orbscan 1W headline ≈ last − first).
    series: list[dict[str, Any]] = []
    for p in points_raw:
        series.append(
            {
                "t": int(p["t"]),
                "pnl": _round_pnl_to_cents(float(p["pnl_abs"]) - base),
                "pnl_abs": _round_pnl_to_cents(float(p["pnl_abs"])),
                "fee": 0.0,
                "reward": 0.0,
                "deposit": 0.0,
                "withdraw": 0.0,
            }
        )

    if series:
        # Assign each cashflow to the next series bucket at/after its timestamp.
        times = [int(p["t"]) for p in series]
        for flow in flows:
            ts = int(flow["timestamp"])
            if cutoff is not None and ts < cutoff:
                continue
            bucket = _cashflow_bucket(str(flow.get("type") or ""))
            if not bucket:
                continue
            # Binary-ish: first index with t >= ts, else last.
            idx = 0
            while idx < len(times) and times[idx] < ts:
                idx += 1
            if idx >= len(times):
                idx = len(times) - 1
            try:
                usd = abs(float(flow.get("usd") or 0.0))
            except (TypeError, ValueError):
                usd = 0.0
            series[idx][bucket] = _round_pnl_to_cents(float(series[idx][bucket]) + usd)

    headline = series[-1]["pnl"] if series else None
    return {
        "wallet": wallet,
        "interval": key,
        "fidelity": fidelity,
        "scope": "account",
        "pnl": headline,
        "start_pnl": series[0]["pnl"] if series else None,
        "end_pnl": headline,
        "series": series,
    }


def _attach_day_rebates(
    daily: list[dict[str, Any]],
    rebates_by_day: dict[str, float],
) -> None:
    for row in daily:
        day = str(row.get("date") or "")
        row["rebates"] = float(rebates_by_day.get(day) or 0.0)


def _slug_activity_bounds(slug: str) -> tuple[int, int] | None:
    """Activity window for an up/down market slug (trades + late redeems)."""
    hit = series_from_slug((slug or "").strip())
    if hit is None:
        return None
    m = _UPDOWN_SLUG_RE.match((slug or "").strip())
    if not m:
        return None
    try:
        start_sec = int(m.group(3))
    except ValueError:
        return None
    if start_sec <= 0:
        return None
    start_ms = start_sec * 1000
    # 2m before open → 30m after window end covers fills + post-window redeems.
    return start_ms - 120_000, start_ms + hit.duration_s * 1000 + 30 * 60_000


def _display_name(row: dict[str, Any], wallet: str) -> str:
    name = str(row.get("name") or "").strip()
    pseudo = str(row.get("pseudonym") or "").strip()
    if name:
        return name
    if pseudo:
        return pseudo
    if len(wallet) > 12:
        return f"{wallet[:6]}...{wallet[-4:]}"
    return wallet or "—"


def _norm_activity(row: dict[str, Any]) -> dict[str, Any] | None:
    ts = _ts_ms(row.get("timestamp"))
    if ts is None:
        return None
    slug = str(row.get("slug") or row.get("eventSlug") or "") or None
    title = str(row.get("title") or "") or None
    if not is_btc_updown_5m(slug=slug, title=title):
        return None
    wallet = str(row.get("proxyWallet") or row.get("proxy_wallet") or "").lower()
    side = str(row.get("side") or "").upper() or None
    typ = str(row.get("type") or "TRADE").upper()
    try:
        price = float(row.get("price")) if row.get("price") is not None else None
    except (TypeError, ValueError):
        price = None
    try:
        size = float(row.get("size") if row.get("size") is not None else row.get("shares") or 0)
    except (TypeError, ValueError):
        size = 0.0
    try:
        usd = float(row.get("usdcSize") if row.get("usdcSize") is not None else (price or 0) * size)
    except (TypeError, ValueError):
        usd = 0.0
    outcome = str(row.get("outcome") or "").strip() or None
    if not outcome:
        try:
            oi = int(row.get("outcomeIndex"))
            outcome = "Up" if oi == 0 else "Down" if oi == 1 else None
        except (TypeError, ValueError):
            outcome = None
    tx = str(row.get("transactionHash") or row.get("transaction_hash") or "") or None
    return {
        "timestamp": ts,
        "type": typ,
        "side": side,
        "outcome": outcome,
        "price": price,
        "shares": size,
        "usd": usd,
        "title": title,
        "slug": slug,
        "condition_id": str(row.get("conditionId") or "") or None,
        "transaction_hash": tx,
        "proxy_wallet": wallet,
        "name": str(row.get("name") or "") or None,
        "pseudonym": str(row.get("pseudonym") or "") or None,
        "icon": str(row.get("icon") or "") or None,
        "polygonscan_url": f"https://polygonscan.com/tx/{tx}" if tx else None,
        "orbscan_url": f"https://orbscan.com/tx/{tx}" if tx else None,
    }


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    retries: int = 4,
) -> Any:
    last_exc: Exception | None = None
    for attempt in range(max(1, int(retries))):
        try:
            resp = await client.get(url, params=params or {}, headers=_headers())
            if resp.status_code == 429:
                wait = min(8.0, 0.4 * (2**attempt))
                await asyncio.sleep(wait)
                last_exc = httpx.HTTPStatusError(
                    f"429 Too Many Requests for {url}",
                    request=resp.request,
                    response=resp,
                )
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                wait = min(8.0, 0.4 * (2**attempt))
                await asyncio.sleep(wait)
                last_exc = exc
                continue
            raise
        except httpx.TransportError as exc:
            wait = min(8.0, 0.3 * (2**attempt))
            await asyncio.sleep(wait)
            last_exc = exc
            continue
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"request failed: {url}")


async def _iter_closed_position_pages(
    client: httpx.AsyncClient,
    wallet: str,
    *,
    page: int = _CLOSED_PAGE,
    max_offset: int = 200_000,
    concurrency: int = _PAGE_CONCURRENCY,
) -> AsyncIterator[list[Any]]:
    """Yield closed-positions pages (TIMESTAMP DESC) fetching `concurrency` ahead."""
    page = max(1, min(int(page), 50))
    concurrency = max(1, min(int(concurrency), 24))
    offset = 0
    while offset <= max_offset:

        async def _one(o: int) -> list[Any] | None:
            try:
                batch = await _get_json(
                    client,
                    f"{DATA_API_URL}/closed-positions",
                    params={
                        "user": wallet,
                        "limit": page,
                        "offset": o,
                        "sortBy": "TIMESTAMP",
                        "sortDirection": "DESC",
                    },
                )
            except httpx.HTTPStatusError:
                return None
            return batch if isinstance(batch, list) else []

        offs = [offset + i * page for i in range(concurrency) if offset + i * page <= max_offset]
        if not offs:
            break
        batches = await asyncio.gather(*[_one(o) for o in offs])
        stop = False
        for batch in batches:
            if batch is None:
                return
            yield batch
            if len(batch) < page:
                stop = True
                break
        if stop:
            return
        offset += len(offs) * page


# Give up on BTC-only scans after this many consecutive non-BTC pages when
# nothing BTC has been found yet (avoids multi-minute crawls on non-BTC wallets).
_EMPTY_BTC_PAGE_LIMIT = 60


def _btc_empty_should_stop(*, pages_seen: int, btc_found: int) -> bool:
    return btc_found <= 0 and pages_seen >= _EMPTY_BTC_PAGE_LIMIT


async def _iter_activity_pages(
    client: httpx.AsyncClient,
    wallet: str,
    *,
    start_ms: int,
    end_ms: int,
    page: int = _ACTIVITY_PAGE,
    max_offset: int = 50_000,
    concurrency: int = _PAGE_CONCURRENCY,
    start_offset: int = 0,
) -> AsyncIterator[list[Any]]:
    """Yield activity pages in [start,end) fetching `concurrency` ahead."""
    page = max(1, min(int(page), 500))
    concurrency = max(1, min(int(concurrency), 24))
    offset = max(0, int(start_offset))
    start_s = max(0, int(start_ms) // 1000)
    end_s = max(start_s + 1, int(end_ms) // 1000)
    while offset <= max_offset:

        async def _one(o: int) -> list[Any] | None:
            try:
                batch = await _get_json(
                    client,
                    f"{DATA_API_URL}/activity",
                    params={
                        "user": wallet,
                        "limit": page,
                        "offset": o,
                        "start": start_s,
                        "end": end_s,
                    },
                )
            except httpx.HTTPStatusError:
                return None
            return batch if isinstance(batch, list) else []

        offs = [offset + i * page for i in range(concurrency) if offset + i * page <= max_offset]
        if not offs:
            break
        batches = await asyncio.gather(*[_one(o) for o in offs])
        stop = False
        for batch in batches:
            if batch is None:
                return
            yield batch
            if len(batch) < page:
                stop = True
                break
        if stop:
            return
        offset += len(offs) * page


async def fetch_wallet_summary(
    address: str, *, series: str | None = "5m"
) -> dict[str, Any]:
    """Profile + BTC Up/Down position/PnL headline stats for the user card."""
    wallet = normalize_wallet(address)
    with use_btc_series(series) as series_key:
        async with _http_client(timeout_s=90.0) as client:
            trades_task = _get_json(
                client,
                f"{DATA_API_URL}/trades",
                params={"user": wallet, "limit": 1, "takerOnly": "false"},
            )

            async def _lb() -> Any:
                try:
                    return await _get_json(
                        client,
                        f"{LB_API_URL}/profit",
                        params={"address": wallet, "window": "all", "limit": 1},
                    )
                except Exception:
                    return []

            trades, lb = await asyncio.gather(trades_task, _lb())

            # Scan closed BTC positions for all-time PnL + biggest win (not all-market LB).
            total_pnl = 0.0
            biggest_win: dict[str, Any] | None = None
            closed_n = 0
            closed_keys: set[str] = set()
            max_btc = 8_000
            pages_seen = 0
            async for batch in _iter_closed_position_pages(client, wallet):
                pages_seen += 1
                for raw in batch:
                    item = _norm_closed_position(raw)
                    if item is None:
                        continue
                    closed_n += 1
                    try:
                        pnl = float(item.get("realized_pnl") or 0.0)
                    except (TypeError, ValueError):
                        pnl = 0.0
                    total_pnl += pnl
                    closed_keys.update(_market_lookup_keys(item))
                    if biggest_win is None or pnl > float(biggest_win["realized_pnl"]):
                        biggest_win = {
                            "realized_pnl": pnl,
                            "title": item.get("title"),
                            "slug": item.get("slug"),
                            "outcome": item.get("outcome"),
                        }
                    if closed_n >= max_btc:
                        break
                if closed_n >= max_btc:
                    break
                if _btc_empty_should_stop(pages_seen=pages_seen, btc_found=closed_n):
                    break

            extras, rebate_rows = await asyncio.gather(
                _btc_unrealized_extras(
                    client, wallet, closed_keys, activity_lookback_ms=45 * 86_400_000
                ),
                _paginate_rebate_activity(client, wallet),
            )

        profile_src: dict[str, Any] = {}
        if isinstance(trades, list) and trades:
            profile_src = trades[0]
        elif isinstance(lb, list) and lb:
            profile_src = lb[0]

        closed_pnl = _round_pnl_to_cents(total_pnl) if closed_n else 0.0
        activity_pnl = float(extras.get("activity_pnl") or 0.0)
        open_pnl = float(extras.get("open_pnl") or 0.0)
        # Same formula as PnL-by-day / PnL-by-market: closed + tape + leftover open marks.
        combined = _round_pnl_to_cents(closed_pnl + activity_pnl + open_pnl)
        rebates = _rebate_rollups(rebate_rows)

        return {
            "wallet": wallet,
            "name": _display_name(profile_src, wallet),
            "pseudonym": str(profile_src.get("pseudonym") or "") or None,
            "profile_image": str(
                profile_src.get("profileImageOptimized")
                or profile_src.get("profileImage")
                or ""
            )
            or None,
            "scope": series_scope(series_key),
            "series": series_key,
            "includes_open": True,
            "markets_aligned": True,
            "positions_value": float(extras.get("positions_value") or 0.0),
            "biggest_win": biggest_win,
            "total_pnl": combined,
            "closed_pnl": closed_pnl,
            "open_pnl": open_pnl,
            "activity_pnl": activity_pnl,
            "total_rebates": rebates["total_rebates"],
            "maker_rebates": rebates["maker_rebates"],
            "taker_rebates": rebates["taker_rebates"],
            "rebate_events": rebates["rebate_events"],
            "open_positions": int(extras.get("n_open") or 0),
            "closed_sample": closed_n,
            "polygonscan_url": f"https://polygonscan.com/address/{wallet}",
            "orbscan_url": f"https://orbscan.com/profile/{wallet}",
            "polymarket_url": f"https://polymarket.com/profile/{wallet}",
        }


async def fetch_wallet_pnl(
    address: str, interval: str = "1d", *, series: str | None = "5m"
) -> dict[str, Any]:
    """BTC Up/Down realized-PnL series + headline for the selected interval.

    Built from closed-position settles (not the all-market user-pnl API).
    """
    wallet = normalize_wallet(address)
    series_key = normalize_series(series)
    token = _active_series.set(series_key)
    try:
        return await _fetch_wallet_pnl_impl(wallet, interval, series_key=series_key)
    finally:
        _active_series.reset(token)


async def _fetch_wallet_pnl_impl(
    wallet: str, interval: str, *, series_key: str
) -> dict[str, Any]:
    key = (interval or "1d").strip().lower()
    if key not in _PNL_INTERVALS:
        raise ValueError(f"Invalid interval (use {', '.join(_PNL_INTERVALS)})")
    _api_interval, fidelity = _PNL_INTERVALS[key]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cutoff = _window_cutoff_ms(key, now_ms=now_ms)

    # Deeper scans for longer windows so older BTC settles are included.
    scan_by_interval = {
        "1d": 2_000,
        "1w": 4_000,
        "1m": 8_000,
        "1y": 20_000,
        "ytd": 20_000,
        "all": 30_000,
        "max": 30_000,
    }
    max_btc = scan_by_interval.get(key, 8_000)

    async with _http_client(timeout_s=90.0) as client:
        # Pull closed BTC rows (newest first), then rebuild chronological cumulative curve.
        closed: list[dict[str, Any]] = []
        exhausted = False
        pages_seen = 0
        async for batch in _iter_closed_position_pages(client, wallet):
            pages_seen += 1
            if not batch:
                exhausted = True
                break
            stop_old = False
            for raw in batch:
                item = _norm_closed_position(raw)
                if item is None:
                    continue
                ts = item.get("timestamp")
                if cutoff is not None and ts is not None and int(ts) < cutoff - 86_400_000:
                    # One extra day of baseline, then stop — TIMESTAMP DESC.
                    stop_old = True
                    break
                closed.append(item)
                if len(closed) >= max_btc:
                    break
            if stop_old or len(closed) >= max_btc:
                break
            if len(batch) < _CLOSED_PAGE:
                exhausted = True
                break
            if _btc_empty_should_stop(pages_seen=pages_seen, btc_found=len(closed)):
                exhausted = True
                break

        open_pnl = 0.0
        open_n = 0
        activity_pnl = 0.0
        closed_keys: set[str] = set()
        for item in closed:
            closed_keys.update(_market_lookup_keys(item))
        extras = await _btc_unrealized_extras(
            client,
            wallet,
            closed_keys,
            cutoff_ms=cutoff,
            now_ms=now_ms,
            activity_lookback_ms=45 * 86_400_000,
        )
        activity_pnl = float(extras.get("activity_pnl") or 0.0)
        open_pnl = float(extras.get("open_pnl") or 0.0)
        open_n = int(extras.get("n_open") or 0)
        extra_pnl = float(extras.get("extra_pnl") or 0.0)

    # Oldest → newest cumulative realized PnL.
    closed.sort(key=lambda r: int(r.get("timestamp") or 0))
    points: list[dict[str, Any]] = []
    cum = 0.0
    for pos in closed:
        ts = pos.get("timestamp")
        if ts is None:
            continue
        try:
            pnl = float(pos.get("realized_pnl") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        cum += pnl
        points.append({"t": int(ts), "pnl": _round_pnl_to_cents(cum)})

    # Collapse to interval fidelity for chart readability.
    if key == "1d":
        # Align buckets with the selected market series window.
        bucket_ms = (15 if series_key == "15m" else 5) * 60_000
    elif key == "1w":
        bucket_ms = 60 * 60_000
    elif key == "1m":
        bucket_ms = 6 * 60 * 60_000
    else:
        bucket_ms = 24 * 60 * 60_000

    if points and bucket_ms > 0:
        bucketed: dict[int, dict[str, Any]] = {}
        for p in points:
            b = (int(p["t"]) // bucket_ms) * bucket_ms
            bucketed[b] = {"t": b, "pnl": p["pnl"]}
        points = [bucketed[k] for k in sorted(bucketed)]

    if cutoff is not None:
        # Keep one baseline point before the window when available.
        pre = [p for p in points if p["t"] < cutoff]
        in_win = [p for p in points if p["t"] >= cutoff]
        if pre and in_win:
            points = [pre[-1], *in_win]
        else:
            points = in_win

    start_pnl = points[0]["pnl"] if points else None
    closed_end_pnl = points[-1]["pnl"] if points else None
    end_pnl = closed_end_pnl

    # Age from first BTC settle.
    age_days = None
    if points:
        age_days = max(0.0, (now_ms - int(points[0]["t"])) / 86_400_000.0)

    use_absolute = key in {"all", "max"} or _period_too_short(key, age_days)
    if closed_end_pnl is None and open_n == 0 and activity_pnl == 0:
        headline = None
    elif use_absolute or start_pnl is None or len(points) < 2:
        base = float(closed_end_pnl or 0.0)
        headline = _round_pnl_to_cents(base + extra_pnl)
    else:
        headline = _round_pnl_to_cents(float(closed_end_pnl) - float(start_pnl) + extra_pnl)

    # Fold unredeemed extras into the series so the chart end matches the headline.
    if extra_pnl and (points or extra_pnl != 0):
        last = float(closed_end_pnl or 0.0) + extra_pnl
        points = [
            *points,
            {"t": now_ms, "pnl": _round_pnl_to_cents(last)},
        ]
        end_pnl = _round_pnl_to_cents(last)

    return {
        "wallet": wallet,
        "interval": key,
        "fidelity": fidelity,
        "scope": series_scope(series_key),
        "series_key": series_key,
        "includes_open": True,
        "markets_aligned": True,
        "start_pnl": start_pnl,
        "end_pnl": end_pnl,
        "pnl": headline,
        "closed_pnl": closed_end_pnl,
        "open_pnl": open_pnl,
        "activity_pnl": activity_pnl,
        "absolute": use_absolute,
        "account_age_days": age_days,
        "has_more": (not exhausted) and len(closed) >= max_btc,
        "n_positions": len(closed),
        "n_open": open_n,
        "series": points,
    }


def _open_position_mark_pnl(pos: dict[str, Any]) -> float:
    """Economic PnL for an open/unredeemed position (matches market tape estimate)."""
    try:
        if pos.get("cash_pnl") is not None:
            return float(pos["cash_pnl"])
    except (TypeError, ValueError):
        pass
    try:
        if pos.get("realized_pnl") is not None:
            return float(pos["realized_pnl"])
    except (TypeError, ValueError):
        pass
    try:
        cur = float(pos.get("current_value") or 0)
    except (TypeError, ValueError):
        cur = 0.0
    try:
        size = float(pos.get("size") or 0)
    except (TypeError, ValueError):
        size = 0.0
    try:
        avg = float(pos["avg_price"]) if pos.get("avg_price") is not None else None
    except (TypeError, ValueError):
        avg = None
    cost = (avg * size) if avg is not None else 0.0
    return cur - cost


def _slug_market_day(slug: str | None) -> str | None:
    """ET calendar day of a BTC 5m market window (from slug open time)."""
    slug_ts = _slug_window_start_ms(slug)
    if slug_ts is None:
        return None
    return datetime.fromtimestamp(int(slug_ts) / 1000, tz=_ET).strftime("%Y-%m-%d")


def _settle_day(pos: dict[str, Any]) -> str | None:
    """Attribute a position to its market-window ET day (not redeem/settle day).

    Midnight windows (e.g. 11:55PM–12:00AM) redeem next calendar day; day buckets
    must still use the slug open day so the market appears once with full PnL.
    """
    day = _slug_market_day(pos.get("slug") if isinstance(pos.get("slug"), str) else None)
    if day:
        return day
    end_date = pos.get("end_date")
    if isinstance(end_date, str) and len(end_date) >= 10:
        return end_date[:10]
    ts = pos.get("timestamp")
    if ts is not None:
        return datetime.fromtimestamp(int(ts) / 1000, tz=_ET).strftime("%Y-%m-%d")
    return None


def _open_market_day(pos: dict[str, Any]) -> str | None:
    """ET calendar day for an open/unredeemed market (same rules as closed)."""
    return _settle_day(pos)


def _row_market_day(row: dict[str, Any]) -> str | None:
    """Best-effort market-window day for a market/activity/closed row."""
    day = _slug_market_day(row.get("slug") if isinstance(row.get("slug"), str) else None)
    if day:
        return day
    end_date = row.get("end_date")
    if isinstance(end_date, str) and len(end_date) >= 10:
        return end_date[:10]
    ts = row.get("timestamp")
    if ts is not None:
        try:
            return datetime.fromtimestamp(int(ts) / 1000, tz=_ET).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            return None
    return None


async def _aggregate_btc_closed_by_day(
    client: httpx.AsyncClient,
    wallet: str,
    *,
    max_btc: int = 3000,
    max_offset: int = 200_000,
    before: str | None = None,
) -> tuple[dict[str, dict[str, Any]], bool, dict[str, set[str]]]:
    """
    Sum realized BTC Up/Down 5m PnL by market-window day.

    Also returns per-day market lookup keys (condition_id / slug) so activity-tape
    extras can skip markets that already have closed PnL.
    """
    max_btc = max(50, min(int(max_btc), 50_000))
    by_day: dict[str, dict[str, Any]] = {}
    closed_keys_by_day: dict[str, set[str]] = {}
    n_btc = 0
    exhausted = False
    pages_seen = 0

    async for batch in _iter_closed_position_pages(
        client, wallet, max_offset=max_offset
    ):
        pages_seen += 1
        if not batch:
            exhausted = True
            break
        for raw in batch:
            item = _norm_closed_position(raw)
            if item is None:
                continue
            day = _settle_day(item)
            if day is None:
                continue
            if before and day >= before:
                continue
            n_btc += 1
            bucket = by_day.get(day)
            pnl = item.get("realized_pnl")
            ts = item.get("timestamp") or 0
            if bucket is None:
                by_day[day] = {
                    "date": day,
                    "pnl": float(pnl or 0.0),
                    "realized_pnl": float(pnl or 0.0),
                    "n_positions": 1,
                    "n_open": 0,
                    "t": int(ts) if ts else 0,
                }
            else:
                bucket["pnl"] = float(bucket["pnl"]) + float(pnl or 0.0)
                bucket["realized_pnl"] = float(bucket["realized_pnl"]) + float(pnl or 0.0)
                bucket["n_positions"] = int(bucket["n_positions"]) + 1
                if ts and int(ts) > int(bucket.get("t") or 0):
                    bucket["t"] = int(ts)
            keys = closed_keys_by_day.setdefault(day, set())
            for key in _market_lookup_keys(item):
                keys.add(key)
            if n_btc >= max_btc:
                break

        if n_btc >= max_btc:
            break
        if len(batch) < _CLOSED_PAGE:
            exhausted = True
            break
        if _btc_empty_should_stop(pages_seen=pages_seen, btc_found=n_btc):
            exhausted = True
            break

    has_more = (not exhausted) and n_btc >= max_btc
    return by_day, has_more, closed_keys_by_day


def _market_lookup_keys(row: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for v in (row.get("condition_id"), row.get("slug")):
        s = str(v or "").strip().lower()
        if s:
            out.append(s)
    return out


async def _paginate_btc_activity_range(
    client: httpx.AsyncClient,
    wallet: str,
    start_ms: int,
    end_ms: int,
    *,
    max_rows: int = 12_000,
) -> list[dict[str, Any]]:
    """BTC 5m activity in [start_ms, end_ms)."""
    rows: list[dict[str, Any]] = []
    max_rows = max(100, min(int(max_rows), 30_000))
    async for batch in _iter_activity_pages(
        client, wallet, start_ms=start_ms, end_ms=end_ms
    ):
        for raw in batch:
            item = _norm_activity(raw)
            if item is None:
                continue
            ts = int(item["timestamp"])
            if ts < start_ms or ts >= end_ms:
                continue
            rows.append(item)
            if len(rows) >= max_rows:
                return rows[:max_rows]
        if len(batch) < _ACTIVITY_PAGE:
            break
    return rows[:max_rows]


async def _btc_unrealized_extras(
    client: httpx.AsyncClient,
    wallet: str,
    closed_keys: set[str],
    *,
    cutoff_ms: int | None = None,
    now_ms: int | None = None,
    activity_lookback_ms: int = 30 * 86_400_000,
) -> dict[str, Any]:
    """
    Activity-tape + open marks for markets not already in closed_keys.

    Matches PnL-by-market / aligned daily: prefer tape, then leftover open marks.
    """
    now = int(now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    if cutoff_ms is not None:
        act_start = int(cutoff_ms)
    else:
        act_start = max(0, now - int(activity_lookback_ms))
    act_end = now + 45 * 60_000

    async def _load_open() -> list[Any]:
        try:
            raw = await _get_json(
                client,
                f"{DATA_API_URL}/positions",
                params={"user": wallet, "limit": 100, "sizeThreshold": 0},
            )
        except httpx.HTTPStatusError:
            return []
        return raw if isinstance(raw, list) else []

    acts, raw_open = await asyncio.gather(
        _paginate_btc_activity_range(
            client, wallet, act_start, act_end, max_rows=12_000
        ),
        _load_open(),
    )
    groups = _group_activity_by_market(acts)
    tape = 0.0
    tape_keys: set[str] = set()
    for m in groups:
        if _is_live_btc_market(m.get("slug"), now_ms=now):
            continue
        slug_ts = _slug_window_start_ms(m.get("slug"))
        acts_rows = list(m.get("activity") or [])
        first_ts = acts_rows[0].get("timestamp") if acts_rows else None
        ts = slug_ts or first_ts or now
        if cutoff_ms is not None and int(ts) < int(cutoff_ms):
            continue
        keys = _market_lookup_keys(m)
        if any(k in closed_keys for k in keys):
            continue
        tp = _activity_tape_pnl(acts_rows)
        if tp is None:
            continue
        tape += float(tp)
        tape_keys.update(keys)

    open_extra = 0.0
    open_n = 0
    positions_value = 0.0
    for raw in raw_open:
        item = _norm_open_position(raw)
        if item is None:
            continue
        if _is_live_btc_market(item.get("slug"), now_ms=now):
            continue
        slug_ts = _slug_window_start_ms(item.get("slug"))
        ts = slug_ts or item.get("timestamp") or now
        if cutoff_ms is not None and int(ts) < int(cutoff_ms):
            continue
        try:
            positions_value += float(item.get("current_value") or 0)
        except (TypeError, ValueError):
            pass
        keys = _market_lookup_keys(item)
        if any(k in closed_keys for k in keys):
            continue
        if any(k in tape_keys for k in keys):
            continue
        open_extra += _open_position_mark_pnl(item)
        open_n += 1

    activity_pnl = _round_pnl_to_cents(tape)
    open_pnl = _round_pnl_to_cents(open_extra)
    return {
        "activity_pnl": activity_pnl,
        "open_pnl": open_pnl,
        "extra_pnl": _round_pnl_to_cents(activity_pnl + open_pnl),
        "n_open": open_n,
        "positions_value": positions_value,
    }


async def _apply_markets_style_day_totals(
    client: httpx.AsyncClient,
    wallet: str,
    daily: list[dict[str, Any]],
    closed_keys_by_day: dict[str, set[str]],
) -> None:
    """
    Set each day's pnl like PnL-by-market: closed settles + activity-tape for
    markets that never got a closed row (unredeemed / not indexed yet).
    """
    if not daily:
        return
    date_set = {str(r["date"]) for r in daily}
    dates_sorted = sorted(date_set)
    start_ms, _ = _et_day_bounds(dates_sorted[0])
    _, end_ms = _et_day_bounds(dates_sorted[-1])
    # Late redeems for the newest day's last windows land just after midnight.
    end_ms += 45 * 60_000

    acts_task = _paginate_btc_activity_range(
        client, wallet, start_ms, end_ms, max_rows=20_000
    )

    async def _load_open() -> list[Any]:
        try:
            raw = await _get_json(
                client,
                f"{DATA_API_URL}/positions",
                params={"user": wallet, "limit": 100, "sizeThreshold": 0},
            )
        except httpx.HTTPStatusError:
            return []
        return raw if isinstance(raw, list) else []

    acts, raw_open = await asyncio.gather(acts_task, _load_open())
    groups = _group_activity_by_market(acts)
    tape_by_day: dict[str, float] = {}
    tape_keys_by_day: dict[str, set[str]] = {}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    for m in groups:
        day = _row_market_day(m)
        if day is None or day not in date_set:
            continue
        if _is_live_btc_market(m.get("slug"), now_ms=now_ms):
            continue
        closed_keys = closed_keys_by_day.get(day) or set()
        keys = _market_lookup_keys(m)
        if any(k in closed_keys for k in keys):
            continue
        tape = _activity_tape_pnl(list(m.get("activity") or []))
        if tape is None:
            continue
        tape_by_day[day] = float(tape_by_day.get(day, 0.0)) + float(tape)
        tape_keys_by_day.setdefault(day, set()).update(keys)

    # Open marks for claimable/unindexed markets missed by the activity window.
    open_extra_by_day: dict[str, float] = {}
    for raw in raw_open:
        item = _norm_open_position(raw)
        if item is None:
            continue
        if _is_live_btc_market(item.get("slug"), now_ms=now_ms):
            continue
        day = _open_market_day(item)
        if day is None or day not in date_set:
            continue
        keys = _market_lookup_keys(item)
        closed_keys = closed_keys_by_day.get(day) or set()
        if any(k in closed_keys for k in keys):
            continue
        tape_keys = tape_keys_by_day.get(day) or set()
        if any(k in tape_keys for k in keys):
            continue
        open_extra_by_day[day] = float(open_extra_by_day.get(day, 0.0)) + float(
            _open_position_mark_pnl(item)
        )
        for row in daily:
            if row["date"] == day:
                row["n_open"] = int(row.get("n_open") or 0) + 1
                break

    for row in daily:
        day = str(row["date"])
        realized = _round_pnl_to_cents(float(row.get("realized_pnl") or 0.0))
        row["realized_pnl"] = realized
        extra = float(tape_by_day.get(day, 0.0)) + float(open_extra_by_day.get(day, 0.0))
        row["pnl"] = _round_pnl_to_cents(realized + extra)
        row.setdefault("n_open", 0)


async def _fold_open_into_daily(
    client: httpx.AsyncClient,
    wallet: str,
    by_day: dict[str, dict[str, Any]],
    *,
    before: str | None = None,
) -> int:
    """Add open/unredeemed mark PnL into ET market-window days. Returns open count."""
    try:
        raw_open = await _get_json(
            client,
            f"{DATA_API_URL}/positions",
            params={"user": wallet, "limit": 100, "sizeThreshold": 0},
        )
    except httpx.HTTPStatusError:
        raw_open = []
    open_n = 0
    if not isinstance(raw_open, list):
        return 0
    for raw in raw_open:
        item = _norm_open_position(raw)
        if item is None:
            continue
        # Skip the in-progress window — not a settled/unredeemed past market.
        if _is_live_btc_market(item.get("slug")):
            continue
        day = _open_market_day(item)
        if day is None:
            continue
        if before and day >= before:
            continue
        mark = _open_position_mark_pnl(item)
        open_n += 1
        slug_ts = _slug_window_start_ms(item.get("slug")) or item.get("timestamp") or 0
        bucket = by_day.get(day)
        if bucket is None:
            by_day[day] = {
                "date": day,
                "pnl": float(mark),
                "realized_pnl": 0.0,
                "n_positions": 0,
                "n_open": 1,
                "t": int(slug_ts) if slug_ts else 0,
            }
        else:
            bucket["pnl"] = float(bucket["pnl"]) + float(mark)
            bucket["n_open"] = int(bucket.get("n_open") or 0) + 1
            if slug_ts and int(slug_ts) > int(bucket.get("t") or 0):
                bucket["t"] = int(slug_ts)
    return open_n


async def fetch_wallet_daily_pnl(
    address: str,
    *,
    days: int = 90,
    scan_limit: int = 3000,
    before: str | None = None,
    series: str | None = "5m",
) -> dict[str, Any]:
    """BTC Up/Down PnL by day (same formula as PnL-by-market totals)."""
    wallet = normalize_wallet(address)
    series_key = normalize_series(series)
    token = _active_series.set(series_key)
    try:
        return await _fetch_wallet_daily_pnl_impl(
            wallet,
            days=days,
            scan_limit=scan_limit,
            before=before,
            series_key=series_key,
        )
    finally:
        _active_series.reset(token)


async def _fetch_wallet_daily_pnl_impl(
    wallet: str,
    *,
    days: int,
    scan_limit: int,
    before: str | None,
    series_key: str,
) -> dict[str, Any]:
    days = max(1, min(int(days), 730))
    scan_limit = max(50, min(int(scan_limit), 50_000))
    if before:
        try:
            datetime.strptime(before, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("before must be YYYY-MM-DD") from exc

    async with _http_client(timeout_s=120.0) as client:
        closed_task = _aggregate_btc_closed_by_day(
            client, wallet, max_btc=scan_limit, before=before
        )
        rebates_task = _paginate_rebate_activity(client, wallet)
        (by_day, has_more, closed_keys_by_day), rebate_rows = await asyncio.gather(
            closed_task, rebates_task
        )

        for bucket in by_day.values():
            bucket["realized_pnl"] = _round_pnl_to_cents(
                float(bucket.get("realized_pnl") or 0.0)
            )
            bucket["pnl"] = bucket["realized_pnl"]
            bucket.setdefault("n_open", 0)

        daily = sorted(by_day.values(), key=lambda r: r["date"], reverse=True)
        daily = daily[:days]
        await _apply_markets_style_day_totals(
            client, wallet, daily, closed_keys_by_day
        )

    rebates = _rebate_rollups(rebate_rows)
    _attach_day_rebates(daily, rebates["rebates_by_day"])

    for row in daily:
        row["cum_pnl"] = None  # BTC-only day sums; not account-level cumulative

    return {
        "wallet": wallet,
        "days": days,
        "scan_limit": scan_limit,
        "before": before,
        "traded_days": len(by_day),
        "has_more": has_more or len(by_day) > len(daily),
        "includes_open": True,
        "by_market_day": True,
        "markets_aligned": True,
        "scope": series_scope(series_key),
        "series": series_key,
        "n_open": 0,
        "total_rebates": rebates["total_rebates"],
        "maker_rebates": rebates["maker_rebates"],
        "taker_rebates": rebates["taker_rebates"],
        "daily": daily,
    }


def _norm_closed_position(row: dict[str, Any]) -> dict[str, Any] | None:
    title = str(row.get("title") or "") or None
    slug = str(row.get("slug") or row.get("eventSlug") or "") or None
    if not is_btc_updown_5m(slug=slug, title=title):
        return None
    cid = str(row.get("conditionId") or row.get("condition_id") or "") or None
    try:
        realized = float(row.get("realizedPnl"))
    except (TypeError, ValueError):
        realized = None
    try:
        total_bought = float(row.get("totalBought") or 0)
    except (TypeError, ValueError):
        total_bought = 0.0
    try:
        avg_price = float(row.get("avgPrice")) if row.get("avgPrice") is not None else None
    except (TypeError, ValueError):
        avg_price = None
    ts = _ts_ms(row.get("timestamp"))
    end_raw = str(row.get("endDate") or "") or None
    end_date = end_raw[:10] if end_raw and len(end_raw) >= 10 else end_raw
    return {
        "condition_id": cid,
        "title": title,
        "slug": slug,
        "icon": str(row.get("icon") or "") or None,
        "outcome": str(row.get("outcome") or "") or None,
        "realized_pnl": realized,
        "total_bought": total_bought,
        "avg_price": avg_price,
        "timestamp": ts,
        "end_date": end_date,
        "status": "closed",
    }


def _norm_open_position(row: dict[str, Any]) -> dict[str, Any] | None:
    title = str(row.get("title") or "") or None
    slug = str(row.get("slug") or row.get("eventSlug") or "") or None
    if not is_btc_updown_5m(slug=slug, title=title):
        return None
    cid = str(row.get("conditionId") or row.get("condition_id") or "") or None
    try:
        cash_pnl = float(row.get("cashPnl")) if row.get("cashPnl") is not None else None
    except (TypeError, ValueError):
        cash_pnl = None
    try:
        realized = float(row.get("realizedPnl")) if row.get("realizedPnl") is not None else None
    except (TypeError, ValueError):
        realized = None
    try:
        current_value = float(row.get("currentValue") or 0)
    except (TypeError, ValueError):
        current_value = 0.0
    try:
        size = float(row.get("size") or 0)
    except (TypeError, ValueError):
        size = 0.0
    try:
        avg_price = float(row.get("avgPrice")) if row.get("avgPrice") is not None else None
    except (TypeError, ValueError):
        avg_price = None
    pnl = cash_pnl if cash_pnl is not None else realized
    return {
        "condition_id": cid,
        "title": title,
        "slug": slug,
        "icon": str(row.get("icon") or "") or None,
        "outcome": str(row.get("outcome") or "") or None,
        "realized_pnl": pnl,
        "cash_pnl": cash_pnl,
        "current_value": current_value,
        "size": size,
        "avg_price": avg_price,
        "timestamp": _ts_ms(row.get("timestamp")),
        "end_date": str(row.get("endDate") or "") or None,
        "status": "open",
        "redeemable": bool(row.get("redeemable")),
    }


def _group_activity_by_market(
    rows: list[dict[str, Any]],
    *,
    pnl_by_condition: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in rows:
        key = str(item.get("condition_id") or item.get("slug") or item.get("title") or "unknown")
        g = groups.get(key)
        if g is None:
            g = {
                "condition_id": item.get("condition_id"),
                "slug": item.get("slug"),
                "title": item.get("title"),
                "icon": item.get("icon"),
                "n_events": 0,
                "volume_usd": 0.0,
                "pnl": None,
                "activity": [],
            }
            groups[key] = g
            order.append(key)
        g["n_events"] += 1
        g["volume_usd"] += float(item.get("usd") or 0)
        g["activity"].append(item)
        if not g.get("title") and item.get("title"):
            g["title"] = item["title"]
        if not g.get("icon") and item.get("icon"):
            g["icon"] = item["icon"]
        if not g.get("slug") and item.get("slug"):
            g["slug"] = item["slug"]

    pnl_map = pnl_by_condition or {}
    out: list[dict[str, Any]] = []
    for key in order:
        g = groups[key]
        cid = str(g.get("condition_id") or "")
        if cid and cid in pnl_map:
            g["pnl"] = pnl_map[cid]
        g["volume_usd"] = round(float(g["volume_usd"]), 4)
        out.append(g)
    return out


async def _paginate_closed_positions(
    client: httpx.AsyncClient,
    wallet: str,
    *,
    date: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch closed positions, optionally filtered to an ET market-window day."""
    limit = max(1, min(int(limit), 500))
    out: list[dict[str, Any]] = []
    start_ms = end_ms = None
    if date:
        start_ms, end_ms = _et_day_bounds(date)

    async for batch in _iter_closed_position_pages(client, wallet):
        stop_early = False
        for raw in batch:
            # Peek timestamp before BTC filter so non-BTC rows don't block day cutoff.
            ts_raw = _ts_ms(raw.get("timestamp"))
            item = _norm_closed_position(raw)
            if item is None:
                if (
                    date
                    and start_ms is not None
                    and ts_raw is not None
                    and int(ts_raw) < start_ms
                ):
                    stop_early = True
                    break
                continue
            if date:
                market_day = _row_market_day(item)
                on_day = market_day == date if market_day else False
                if not on_day:
                    # TIMESTAMP DESC — once we're fully before the day, stop.
                    # Late redeems for prior-evening windows can timestamp after
                    # midnight; skip those (wrong market day) without stopping.
                    ts = item.get("timestamp")
                    if ts is not None and start_ms is not None and int(ts) < start_ms:
                        stop_early = True
                        break
                    continue
            out.append(item)
            if len(out) >= limit:
                break

        if stop_early or len(out) >= limit or len(batch) < _CLOSED_PAGE:
            break

    return out[:limit]


def _slug_window_start_ms(slug: str | None) -> int | None:
    bounds = _slug_activity_bounds(str(slug or ""))
    if bounds is None:
        return None
    # bounds start is 2m before open; recover official open.
    return int(bounds[0]) + 120_000


def _is_live_btc_market(slug: str | None, *, now_ms: int | None = None) -> bool:
    """True while the market window is still open (exclude from day/market lists)."""
    start = _slug_window_start_ms(slug)
    if start is None:
        return False
    dur_s = slug_duration_s(slug) or 300
    now = int(
        now_ms
        if now_ms is not None
        else datetime.now(timezone.utc).timestamp() * 1000
    )
    return now < start + dur_s * 1000


def _activity_tape_pnl(rows: list[dict[str, Any]]) -> float | None:
    """Net from activity tape: sells + redeems − buys (matches Activity Profit)."""
    if not rows:
        return None
    buy_usd = 0.0
    sell_usd = 0.0
    redeem_usd = 0.0
    for row in rows:
        typ = str(row.get("type") or "").upper()
        side = str(row.get("side") or "").upper()
        try:
            shares = float(row.get("shares") or 0)
        except (TypeError, ValueError):
            shares = 0.0
        try:
            usd = float(row.get("usd") or 0)
        except (TypeError, ValueError):
            usd = 0.0
        if typ == "REDEEM":
            # Losing-side redeems often report usdcSize=0.
            redeem_usd += max(0.0, usd)
            continue
        if typ == "SPLIT":
            buy_usd += usd if usd > 0 else shares
            continue
        if typ == "MERGE":
            sell_usd += usd if usd > 0 else shares
            continue
        if side == "SELL" or typ == "SELL":
            sell_usd += usd
            continue
        if side == "BUY" or typ in {"TRADE", "BUY"} or not side:
            buy_usd += usd
    if buy_usd == 0 and sell_usd == 0 and redeem_usd == 0:
        return None
    return _round_pnl_to_cents(sell_usd + redeem_usd - buy_usd)


def _merge_market_row(
    by_cid: dict[str, dict[str, Any]],
    *,
    condition_id: str | None,
    title: str | None,
    slug: str | None,
    icon: str | None,
    outcome: str | None = None,
    pnl: float | None = None,
    status: str | None = None,
    timestamp: int | None = None,
    end_date: str | None = None,
    total_bought: float = 0.0,
    pnl_mode: str = "add",  # add | fill
) -> None:
    cid = str(condition_id or "")
    key = cid or f"{slug}|{title}|{outcome or ''}"
    cur = by_cid.get(key)
    if cur is None:
        by_cid[key] = {
            "condition_id": condition_id,
            "title": title,
            "slug": slug,
            "icon": icon,
            "outcomes": [outcome] if outcome else [],
            "pnl": float(pnl) if pnl is not None else 0.0,
            "has_pnl": pnl is not None,
            "status": status,
            "timestamp": timestamp,
            "end_date": end_date,
            "total_bought": float(total_bought or 0),
        }
        return
    if pnl is not None:
        if not cur.get("has_pnl"):
            cur["pnl"] = float(pnl)
            cur["has_pnl"] = True
        elif pnl_mode == "add":
            cur["pnl"] = float(cur["pnl"]) + float(pnl)
    if outcome and outcome not in cur["outcomes"]:
        cur["outcomes"].append(outcome)
    if pnl_mode == "add":
        cur["total_bought"] = float(cur["total_bought"]) + float(total_bought or 0)
    elif float(total_bought or 0) > float(cur.get("total_bought") or 0):
        cur["total_bought"] = float(total_bought or 0)
    if status == "closed":
        cur["status"] = "closed"
    elif not cur.get("status") and status:
        cur["status"] = status
    if title and not cur.get("title"):
        cur["title"] = title
    if slug and not cur.get("slug"):
        cur["slug"] = slug
    if icon and not cur.get("icon"):
        cur["icon"] = icon
    if end_date and not cur.get("end_date"):
        cur["end_date"] = end_date
    if timestamp is not None and (
        cur.get("timestamp") is None or int(timestamp) > int(cur["timestamp"])
    ):
        cur["timestamp"] = timestamp


async def fetch_wallet_markets(
    address: str,
    *,
    date: str | None = None,
    limit: int = 100,
    include_open: bool = True,
    activity_limit: int | None = None,
    activity_payload: dict[str, Any] | None = None,
    series: str | None = "5m",
) -> dict[str, Any]:
    """Per-market PnL from closed positions, merged with same-day activity markets."""
    series_key = normalize_series(series)
    token = _active_series.set(series_key)
    try:
        return await _fetch_wallet_markets_impl(
            address,
            date=date,
            limit=limit,
            include_open=include_open,
            activity_limit=activity_limit,
            activity_payload=activity_payload,
            series_key=series_key,
        )
    finally:
        _active_series.reset(token)


async def _fetch_wallet_markets_impl(
    address: str,
    *,
    date: str | None,
    limit: int,
    include_open: bool,
    activity_limit: int | None,
    activity_payload: dict[str, Any] | None,
    series_key: str,
) -> dict[str, Any]:
    wallet = normalize_wallet(address)
    limit = max(1, min(int(limit), 500))
    act_limit = max(limit, min(int(activity_limit or max(limit * 4, 500)), 1000))

    async with _http_client(timeout_s=90.0) as client:
        closed_task = _paginate_closed_positions(
            client, wallet, date=date, limit=max(limit, 500) if date else limit
        )

        async def _load_activity() -> list[dict[str, Any]]:
            if not date:
                return []
            if isinstance(activity_payload, dict) and activity_payload.get("markets") is not None:
                cached_series = activity_payload.get("series")
                if cached_series is None and activity_payload.get("scope") == "btc_updown_15m":
                    cached_series = "15m"
                elif cached_series is None and activity_payload.get("scope") == "btc_updown_5m":
                    cached_series = "5m"
                if cached_series in (None, series_key):
                    return list(activity_payload.get("markets") or [])
            act = await fetch_wallet_activity(
                address, date=date, limit=act_limit, series=series_key, _client=client
            )
            return list(act.get("markets") or [])

        async def _load_open() -> list[dict[str, Any]]:
            if not include_open:
                return []
            try:
                raw_open = await _get_json(
                    client,
                    f"{DATA_API_URL}/positions",
                    params={
                        "user": wallet,
                        "limit": min(100, limit) if not date else 100,
                        "sizeThreshold": 0,
                    },
                )
            except httpx.HTTPStatusError:
                return []
            if not isinstance(raw_open, list):
                return []
            out: list[dict[str, Any]] = []
            for raw in raw_open:
                item = _norm_open_position(raw)
                if item is not None:
                    out.append(item)
            return out

        closed, activity_markets, open_rows = await asyncio.gather(
            closed_task, _load_activity(), _load_open()
        )
        if date:
            # Keep only markets whose slug window opens on this ET day (so a
            # post-midnight redeem does not invent a second-day duplicate).
            activity_markets = [
                m for m in activity_markets if (_row_market_day(m) or date) == date
            ]

    by_cid: dict[str, dict[str, Any]] = {}
    # On a calendar day, closed settles + activity define the list. Open rows are
    # annotation-only so unredeemed losers don't inject extra markets / cash PnL.
    merge_rows = closed if date else (list(closed) + list(open_rows))
    for row in merge_rows:
        _merge_market_row(
            by_cid,
            condition_id=row.get("condition_id"),
            title=row.get("title"),
            slug=row.get("slug"),
            icon=row.get("icon"),
            outcome=row.get("outcome"),
            pnl=row.get("realized_pnl"),
            status=row.get("status"),
            timestamp=row.get("timestamp"),
            end_date=row.get("end_date"),
            total_bought=float(row.get("total_bought") or row.get("size") or 0),
        )

    for m in activity_markets:
        first_ts = None
        acts = m.get("activity") or []
        if acts:
            first_ts = acts[0].get("timestamp")
        _merge_market_row(
            by_cid,
            condition_id=m.get("condition_id"),
            title=m.get("title"),
            slug=m.get("slug"),
            icon=m.get("icon"),
            pnl=m.get("pnl"),
            status="traded",
            timestamp=first_ts,
            total_bought=float(m.get("volume_usd") or 0),
            pnl_mode="fill",
        )

    # Index activity tapes for markets that never got a closed-position PnL
    # (still open / not indexed yet). Fall back to sell+redeem−buy from fills.
    act_by_id: dict[str, list[dict[str, Any]]] = {}
    for m in activity_markets:
        acts = list(m.get("activity") or [])
        if not acts:
            continue
        for key in (
            str(m.get("condition_id") or "").lower(),
            str(m.get("slug") or "").lower(),
        ):
            if key:
                act_by_id[key] = acts

    markets = list(by_cid.values())
    if date:
        markets = [m for m in markets if (_row_market_day(m) or date) == date]
    for m in markets:
        if not m.get("has_pnl"):
            acts = None
            for key in (
                str(m.get("condition_id") or "").lower(),
                str(m.get("slug") or "").lower(),
            ):
                if key and key in act_by_id:
                    acts = act_by_id[key]
                    break
            tape_pnl = _activity_tape_pnl(acts or [])
            if tape_pnl is not None:
                m["pnl"] = tape_pnl
                m["has_pnl"] = True
                m["pnl_source"] = "activity"
        if not m.get("has_pnl"):
            m["pnl"] = None
            m["pnl_source"] = "none"
        else:
            m["pnl"] = _round_pnl_to_cents(float(m["pnl"]))
            m.setdefault("pnl_source", "closed")
        m.pop("has_pnl", None)
        m.setdefault("unredeemed", False)
        m.setdefault("redeemable", False)
        m.setdefault("open_shares", None)
        m.setdefault("open_value", None)

    # Annotate markets with claimable open value (won but not redeemed).
    # Do NOT flag $0 losing shares left unclaimed — Polymarket keeps those as
    # "open", but labeling them unredeemed looks like broken/incomplete PnL.
    open_by_key: dict[str, dict[str, Any]] = {}
    for row in open_rows:
        try:
            open_value = float(row.get("current_value") or 0)
        except (TypeError, ValueError):
            open_value = 0.0
        payload = {
            "unredeemed": True,
            "redeemable": bool(row.get("redeemable")),
            "open_shares": float(row.get("size") or 0),
            "open_value": open_value,
            "open_outcome": row.get("outcome"),
        }
        for key in (
            str(row.get("condition_id") or "").lower(),
            str(row.get("slug") or "").lower(),
        ):
            if key:
                prev = open_by_key.get(key)
                if prev is None:
                    open_by_key[key] = dict(payload)
                else:
                    prev["open_shares"] = float(prev.get("open_shares") or 0) + float(
                        payload["open_shares"] or 0
                    )
                    prev["open_value"] = float(prev.get("open_value") or 0) + float(
                        payload["open_value"] or 0
                    )
                    prev["redeemable"] = bool(prev.get("redeemable") or payload["redeemable"])

    for m in markets:
        hit = None
        for key in (
            str(m.get("condition_id") or "").lower(),
            str(m.get("slug") or "").lower(),
        ):
            if key and key in open_by_key:
                hit = open_by_key[key]
                break
        if not hit:
            continue
        open_value = float(hit.get("open_value") or 0)
        # Worthless losing inventory: treat as settled loss, no unredeemed chrome.
        if open_value <= 0.005:
            continue
        m["unredeemed"] = True
        m["redeemable"] = bool(hit.get("redeemable"))
        m["open_shares"] = round(float(hit.get("open_shares") or 0), 4)
        m["open_value"] = _round_pnl_to_cents(open_value)
        if m.get("status") != "closed":
            m["status"] = "unredeemed"

    # Never list the live in-progress 5m window (open mark / partial tape).
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    markets = [m for m in markets if not _is_live_btc_market(m.get("slug"), now_ms=now_ms)]

    def _sort_ts(row: dict[str, Any]) -> int:
        slug_ts = _slug_window_start_ms(row.get("slug"))
        if slug_ts is not None:
            return slug_ts
        try:
            return int(row.get("timestamp") or 0)
        except (TypeError, ValueError):
            return 0

    markets.sort(
        key=lambda m: (
            -_sort_ts(m),
            -(abs(m["pnl"]) if m.get("pnl") is not None else -1),
        )
    )

    clipped = markets[:limit]
    closed_pnl = sum(
        float(m["pnl"])
        for m in clipped
        if m.get("pnl") is not None and m.get("pnl_source") == "closed"
    )
    activity_pnl = sum(
        float(m["pnl"])
        for m in clipped
        if m.get("pnl") is not None and m.get("pnl_source") == "activity"
    )
    combined = closed_pnl + activity_pnl
    # Header total = closed settles + unredeemed/open tape estimates.
    return {
        "wallet": wallet,
        "date": date,
        "count": len(clipped),
        "total_count": len(markets),
        "has_more": len(markets) > len(clipped),
        "total_pnl": _round_pnl_to_cents(combined) if clipped else 0.0,
        "closed_pnl": _round_pnl_to_cents(closed_pnl) if clipped else 0.0,
        "activity_pnl": _round_pnl_to_cents(activity_pnl) if clipped else 0.0,
        "scope": series_scope(series_key),
        "series": series_key,
        "markets": clipped,
    }


async def fetch_wallet_activity(
    address: str,
    *,
    date: str | None = None,
    slug: str | None = None,
    limit: int = 200,
    offset: int = 0,
    series: str | None = "5m",
    _client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    series_key = normalize_series(series)
    if slug:
        hit = slug_series(slug)
        if hit:
            series_key = hit
    token = _active_series.set(series_key)
    try:
        out = await _fetch_wallet_activity_body(
            address,
            date=date,
            slug=slug,
            limit=limit,
            offset=offset,
            series_key=series_key,
            _client=_client,
        )
        return out
    finally:
        _active_series.reset(token)


async def _fetch_wallet_activity_body(
    address: str,
    *,
    date: str | None = None,
    slug: str | None = None,
    limit: int = 200,
    offset: int = 0,
    series_key: str = "5m",
    _client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    wallet = normalize_wallet(address)
    limit = max(1, min(int(limit), 1000))
    offset = max(0, min(int(offset), 20_000))
    want_slug = (slug or "").strip() or None
    if want_slug and slug_series(want_slug) is None:
        raise ValueError("slug must be a btc-updown-5m/15m or bnb-updown-15m market")

    params: dict[str, Any] = {"user": wallet, "limit": min(500, limit)}
    start_ms = end_ms = None
    # Slug window beats calendar day: closed PnL can settle next ET day while
    # fills happened during the prior evening market window.
    if want_slug:
        bounds = _slug_activity_bounds(want_slug)
        if bounds is None:
            raise ValueError("invalid up/down market slug")
        start_ms, end_ms = bounds
        params["start"] = start_ms // 1000
        params["end"] = end_ms // 1000
    elif date:
        start_ms, end_ms = _et_day_bounds(date)
        params["start"] = start_ms // 1000
        params["end"] = end_ms // 1000

    rows: list[dict[str, Any]] = []
    page_offset = offset
    owns_client = _client is None

    async def _run(client: httpx.AsyncClient) -> dict[str, Any]:
        nonlocal page_offset
        closed_task: asyncio.Task[list[dict[str, Any]]] | None = None
        if date and not want_slug:
            closed_task = asyncio.create_task(
                _paginate_closed_positions(client, wallet, date=date, limit=500)
            )

        # Parallel activity pages when we have a time window; else single-page loop.
        if start_ms is not None and end_ms is not None:
            consumed = 0
            async for batch in _iter_activity_pages(
                client,
                wallet,
                start_ms=start_ms,
                end_ms=end_ms,
                start_offset=offset,
                max_offset=offset + 5000,
            ):
                for raw in batch:
                    item = _norm_activity(raw)
                    if item is None:
                        continue
                    if want_slug and str(item.get("slug") or "").lower() != want_slug.lower():
                        continue
                    if item["timestamp"] < start_ms:
                        continue
                    if item["timestamp"] >= end_ms:
                        continue
                    rows.append(item)
                consumed += len(batch)
                page_offset = offset + consumed
                if len(rows) >= limit or len(batch) < _ACTIVITY_PAGE:
                    break
                if consumed >= 5000:
                    break
        else:
            while len(rows) < limit:
                page_limit = min(500, limit - len(rows))
                page_params = {**params, "limit": page_limit, "offset": page_offset}
                try:
                    batch = await _get_json(
                        client, f"{DATA_API_URL}/activity", params=page_params
                    )
                except httpx.HTTPStatusError:
                    break
                if not isinstance(batch, list) or not batch:
                    break
                for raw in batch:
                    item = _norm_activity(raw)
                    if item is None:
                        continue
                    rows.append(item)
                page_offset += len(batch)
                if len(batch) < page_limit:
                    break
                if page_offset >= offset + 5000:
                    break

        pnl_by_condition: dict[str, float] = {}
        if closed_task is not None:
            closed = await closed_task
            for pos in closed:
                cid = str(pos.get("condition_id") or "")
                if not cid or pos.get("realized_pnl") is None:
                    continue
                pnl_by_condition[cid] = pnl_by_condition.get(cid, 0.0) + float(
                    pos["realized_pnl"]
                )
            for cid, v in list(pnl_by_condition.items()):
                pnl_by_condition[cid] = _round_pnl_to_cents(v)

        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        name = None
        for r in rows:
            if r.get("name"):
                name = r["name"]
                break

        clipped = rows[:limit]
        markets = _group_activity_by_market(clipped, pnl_by_condition=pnl_by_condition)
        # Use the raw Data API offset we actually consumed. BTC filtering means
        # page_offset >> len(clipped); returning len(clipped) made "Fetch more"
        # re-read the same window and miss REDEEM / older fills.
        has_more = len(rows) >= limit
        return {
            "wallet": wallet,
            "date": date,
            "slug": want_slug,
            "count": len(clipped),
            "offset": offset,
            "next_offset": page_offset if page_offset > offset else offset + len(clipped),
            "api_offset": page_offset,
            "has_more": has_more,
            "name": name,
            "scope": series_scope(series_key),
            "series": series_key,
            "activity": clipped,
            "markets": markets,
        }

    if owns_client:
        async with _http_client(timeout_s=90.0) as client:
            return await _run(client)
    assert _client is not None
    return await _run(_client)
