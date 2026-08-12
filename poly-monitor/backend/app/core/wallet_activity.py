"""Cross-market wallet profile / activity via Polymarket public APIs."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

DATA_API_URL = "https://data-api.polymarket.com"
PNL_API_URL = "https://user-pnl-api.polymarket.com"
LB_API_URL = "https://lb-api.polymarket.com"

_ET = ZoneInfo("America/New_York")
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_BTC_UPDOWN_5M_SLUG_RE = re.compile(r"^btc-updown-5m-\d+$", re.I)
_BTC_UPDOWN_5M_TITLE_RE = re.compile(r"^bitcoin\s+up\s+or\s+down\b", re.I)

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


def is_btc_updown_5m(*, slug: str | None = None, title: str | None = None) -> bool:
    """True for BTC Up/Down 5m markets only (excludes ETH and other series)."""
    s = str(slug or "").strip()
    if s:
        return bool(_BTC_UPDOWN_5M_SLUG_RE.match(s))
    t = str(title or "").strip()
    # Title fallback when slug/eventSlug is missing.
    return bool(t and _BTC_UPDOWN_5M_TITLE_RE.match(t))


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
) -> Any:
    resp = await client.get(url, params=params or {}, headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def fetch_wallet_summary(address: str) -> dict[str, Any]:
    wallet = normalize_wallet(address)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0)) as client:
        trades = await _get_json(
            client,
            f"{DATA_API_URL}/trades",
            params={"user": wallet, "limit": 1, "takerOnly": "false"},
        )
        closed = await _get_json(
            client,
            f"{DATA_API_URL}/closed-positions",
            params={
                "user": wallet,
                "limit": 50,
                "sortBy": "REALIZEDPNL",
                "sortDirection": "DESC",
            },
        )
        positions = await _get_json(
            client,
            f"{DATA_API_URL}/positions",
            params={"user": wallet, "limit": 100, "sizeThreshold": 0},
        )
        try:
            lb = await _get_json(
                client,
                f"{LB_API_URL}/profit",
                params={"address": wallet, "window": "all", "limit": 1},
            )
        except Exception:
            lb = []

    profile_src: dict[str, Any] = {}
    if isinstance(trades, list) and trades:
        profile_src = trades[0]
    elif isinstance(lb, list) and lb:
        profile_src = lb[0]

    biggest_win = None
    if isinstance(closed, list):
        for top in closed:
            slug = str(top.get("slug") or top.get("eventSlug") or "") or None
            title = str(top.get("title") or "") or None
            if not is_btc_updown_5m(slug=slug, title=title):
                continue
            try:
                pnl = float(top.get("realizedPnl") or 0)
            except (TypeError, ValueError):
                pnl = 0.0
            biggest_win = {
                "realized_pnl": pnl,
                "title": top.get("title"),
                "slug": top.get("slug") or top.get("eventSlug"),
                "outcome": top.get("outcome"),
            }
            break

    total_pnl = None
    if isinstance(lb, list) and lb:
        try:
            total_pnl = float(lb[0].get("amount"))
        except (TypeError, ValueError):
            total_pnl = None

    open_btc = [
        p
        for p in (positions if isinstance(positions, list) else [])
        if is_btc_updown_5m(
            slug=str(p.get("slug") or p.get("eventSlug") or "") or None,
            title=str(p.get("title") or "") or None,
        )
    ]
    closed_btc = [
        p
        for p in (closed if isinstance(closed, list) else [])
        if is_btc_updown_5m(
            slug=str(p.get("slug") or p.get("eventSlug") or "") or None,
            title=str(p.get("title") or "") or None,
        )
    ]
    open_n = len(open_btc)
    closed_n = len(closed_btc)
    positions_value = 0.0
    for p in open_btc:
        try:
            positions_value += float(p.get("currentValue") or 0)
        except (TypeError, ValueError):
            pass

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
        "positions_value": positions_value,
        "biggest_win": biggest_win,
        "total_pnl": total_pnl,
        "open_positions": open_n,
        "closed_sample": closed_n,
        "polygonscan_url": f"https://polygonscan.com/address/{wallet}",
        "orbscan_url": f"https://orbscan.com/profile/{wallet}",
        "polymarket_url": f"https://polymarket.com/profile/{wallet}",
    }


async def fetch_wallet_pnl(address: str, interval: str = "1d") -> dict[str, Any]:
    """PnL series + headline number using Polymarket profile chart math.

    Polymarket (profile portfolio chart):
      - fetch user-pnl with interval/fidelity
      - keep points with t >= now - window (rolling 1d/1w/1m/1y, or YTD)
      - headline = last.p                 for ALL, or if account younger than window
      - headline = last.p - first.p      otherwise
      - round to cents
    """
    wallet = normalize_wallet(address)
    key = (interval or "1d").strip().lower()
    if key not in _PNL_INTERVALS:
        raise ValueError(f"Invalid interval (use {', '.join(_PNL_INTERVALS)})")
    api_interval, fidelity = _PNL_INTERVALS[key]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0)) as client:
        series = await _get_json(
            client,
            f"{PNL_API_URL}/user-pnl",
            params={
                "user_address": wallet,
                "interval": api_interval,
                "fidelity": fidelity,
            },
        )
        # Account age comes from the all-time curve (same as Polymarket's ALL query).
        age_days: float | None = None
        if key not in {"all", "max"}:
            try:
                all_series = await _get_json(
                    client,
                    f"{PNL_API_URL}/user-pnl",
                    params={
                        "user_address": wallet,
                        "interval": "all",
                        "fidelity": "1d",
                    },
                )
                all_points: list[dict[str, Any]] = []
                if isinstance(all_series, list):
                    for p in all_series:
                        try:
                            t = int(p.get("t") or 0)
                            v = float(p.get("p"))
                        except (TypeError, ValueError):
                            continue
                        if t <= 0:
                            continue
                        ts_ms = t * 1000 if t < 10_000_000_000 else t
                        all_points.append({"t": ts_ms, "pnl": v})
                age_days = _account_age_days(all_points, now_ms=now_ms)
            except Exception:
                age_days = None

    points: list[dict[str, Any]] = []
    if isinstance(series, list):
        for p in series:
            try:
                t = int(p.get("t") or 0)
                v = float(p.get("p"))
            except (TypeError, ValueError):
                continue
            if t <= 0:
                continue
            # API returns epoch seconds
            ts_ms = t * 1000 if t < 10_000_000_000 else t
            points.append({"t": ts_ms, "pnl": v})

    cutoff = _window_cutoff_ms(key, now_ms=now_ms)
    if cutoff is not None:
        filtered = [p for p in points if p["t"] >= cutoff]
        # Keep one baseline point before the window when available so the first
        # delta reflects change from the window start (API buckets can start mid-window).
        # Polymarket filters strictly with >= cutoff; match that exactly.
        points = filtered

    start_pnl = points[0]["pnl"] if points else None
    end_pnl = points[-1]["pnl"] if points else None

    use_absolute = key in {"all", "max"} or _period_too_short(key, age_days)
    if end_pnl is None:
        headline = None
    elif use_absolute or start_pnl is None or len(points) < 2:
        headline = end_pnl
    else:
        headline = end_pnl - start_pnl

    if headline is not None:
        headline = _round_pnl_to_cents(headline)

    return {
        "wallet": wallet,
        "interval": key,
        "fidelity": fidelity,
        "start_pnl": start_pnl,
        "end_pnl": end_pnl,
        "pnl": headline,
        "absolute": use_absolute,
        "account_age_days": age_days,
        "series": points,
    }


async def _collect_btc_traded_days(
    client: httpx.AsyncClient,
    wallet: str,
    *,
    lookback_days: int = 180,
) -> set[str]:
    """ET calendar days where the wallet traded BTC Up/Down 5m."""
    now = datetime.now(timezone.utc)
    cutoff_ms = int((now - timedelta(days=max(1, lookback_days))).timestamp() * 1000)
    traded: set[str] = set()
    offset = 0

    while offset <= 10_000:
        try:
            batch = await _get_json(
                client,
                f"{DATA_API_URL}/trades",
                params={
                    "user": wallet,
                    "limit": 500,
                    "offset": offset,
                    "takerOnly": "false",
                },
            )
        except httpx.HTTPStatusError:
            break
        if not isinstance(batch, list) or not batch:
            break

        stop = False
        for raw in batch:
            slug = str(raw.get("slug") or raw.get("eventSlug") or "") or None
            title = str(raw.get("title") or "") or None
            if not is_btc_updown_5m(slug=slug, title=title):
                continue
            ts = _ts_ms(raw.get("timestamp"))
            if ts is None:
                continue
            if ts < cutoff_ms:
                stop = True
                continue
            traded.add(datetime.fromtimestamp(ts / 1000, tz=_ET).strftime("%Y-%m-%d"))

        if stop or len(batch) < 500:
            break
        offset += len(batch)

    # Also include settle days from closed BTC positions (covers redeems without a fresh trade print).
    closed = await _paginate_closed_positions(client, wallet, date=None, limit=500)
    for pos in closed:
        end_date = pos.get("end_date")
        if isinstance(end_date, str) and len(end_date) >= 10:
            traded.add(end_date[:10])
        ts = pos.get("timestamp")
        if ts is not None:
            traded.add(datetime.fromtimestamp(int(ts) / 1000, tz=_ET).strftime("%Y-%m-%d"))

    return traded


async def fetch_wallet_daily_pnl(address: str, *, days: int = 90) -> dict[str, Any]:
    """Day-over-day PnL deltas, only for days with BTC Up/Down 5m trades."""
    wallet = normalize_wallet(address)
    days = max(1, min(int(days), 730))

    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=8.0)) as client:
        series = await _get_json(
            client,
            f"{PNL_API_URL}/user-pnl",
            params={"user_address": wallet, "interval": "all", "fidelity": "1d"},
        )
        traded_days = await _collect_btc_traded_days(
            client, wallet, lookback_days=max(days + 14, 30)
        )

    points: list[tuple[int, float]] = []
    if isinstance(series, list):
        for p in series:
            try:
                t = int(p.get("t") or 0)
                v = float(p.get("p"))
            except (TypeError, ValueError):
                continue
            if t <= 0:
                continue
            ts_ms = t * 1000 if t < 10_000_000_000 else t
            points.append((ts_ms, v))
    points.sort(key=lambda x: x[0])

    daily: list[dict[str, Any]] = []
    for i in range(1, len(points)):
        prev_t, prev_p = points[i - 1]
        t, p = points[i]
        day = datetime.fromtimestamp(t / 1000, tz=_ET).strftime("%Y-%m-%d")
        if day not in traded_days:
            continue
        daily.append(
            {
                "date": day,
                "t": t,
                "pnl": p - prev_p,
                "cum_pnl": p,
                "prev_t": prev_t,
            }
        )

    # Newest first; keep at most `days` traded days.
    daily.reverse()
    daily = daily[:days]
    return {
        "wallet": wallet,
        "days": days,
        "traded_days": len(traded_days),
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
    end_date = str(row.get("endDate") or "") or None
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
    """Fetch closed positions, optionally filtered to an ET endDate / settle day."""
    limit = max(1, min(int(limit), 500))
    out: list[dict[str, Any]] = []
    offset = 0
    start_ms = end_ms = None
    if date:
        start_ms, end_ms = _et_day_bounds(date)

    while len(out) < limit and offset <= 100_000:
        page = min(50, limit - len(out))
        try:
            batch = await _get_json(
                client,
                f"{DATA_API_URL}/closed-positions",
                params={
                    "user": wallet,
                    "limit": page,
                    "offset": offset,
                    "sortBy": "TIMESTAMP",
                    "sortDirection": "DESC",
                },
            )
        except httpx.HTTPStatusError:
            break
        if not isinstance(batch, list) or not batch:
            break

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
                end_date = item.get("end_date")
                ts = item.get("timestamp")
                on_day = end_date == date
                if not on_day and ts is not None and start_ms is not None and end_ms is not None:
                    on_day = start_ms <= int(ts) < end_ms
                if not on_day:
                    # TIMESTAMP DESC — once we're fully before the day, stop.
                    if ts is not None and start_ms is not None and int(ts) < start_ms:
                        stop_early = True
                        break
                    continue
            out.append(item)
            if len(out) >= limit:
                break

        if stop_early or len(batch) < page:
            break
        offset += len(batch)

    return out[:limit]


async def fetch_wallet_markets(
    address: str,
    *,
    date: str | None = None,
    limit: int = 100,
    include_open: bool = True,
) -> dict[str, Any]:
    """Per-market PnL from closed (and optionally open) positions."""
    wallet = normalize_wallet(address)
    limit = max(1, min(int(limit), 500))

    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=8.0)) as client:
        closed = await _paginate_closed_positions(
            client, wallet, date=date, limit=limit
        )
        open_rows: list[dict[str, Any]] = []
        if include_open and not date:
            try:
                raw_open = await _get_json(
                    client,
                    f"{DATA_API_URL}/positions",
                    params={"user": wallet, "limit": min(100, limit), "sizeThreshold": 0},
                )
            except httpx.HTTPStatusError:
                raw_open = []
            if isinstance(raw_open, list):
                for raw in raw_open:
                    item = _norm_open_position(raw)
                    if item is not None:
                        open_rows.append(item)

    # Aggregate by condition_id (a market can have multiple outcome rows).
    by_cid: dict[str, dict[str, Any]] = {}
    for row in closed + open_rows:
        cid = str(row.get("condition_id") or "")
        key = cid or f"{row.get('slug')}|{row.get('title')}|{row.get('outcome')}"
        cur = by_cid.get(key)
        pnl = row.get("realized_pnl")
        if cur is None:
            by_cid[key] = {
                "condition_id": row.get("condition_id"),
                "title": row.get("title"),
                "slug": row.get("slug"),
                "icon": row.get("icon"),
                "outcomes": [row.get("outcome")] if row.get("outcome") else [],
                "pnl": float(pnl) if pnl is not None else 0.0,
                "has_pnl": pnl is not None,
                "status": row.get("status"),
                "timestamp": row.get("timestamp"),
                "end_date": row.get("end_date"),
                "total_bought": float(row.get("total_bought") or row.get("size") or 0),
            }
        else:
            if pnl is not None:
                cur["pnl"] = float(cur["pnl"]) + float(pnl)
                cur["has_pnl"] = True
            if row.get("outcome") and row.get("outcome") not in cur["outcomes"]:
                cur["outcomes"].append(row.get("outcome"))
            cur["total_bought"] = float(cur["total_bought"]) + float(
                row.get("total_bought") or row.get("size") or 0
            )
            # Prefer closed over open when mixed
            if row.get("status") == "closed":
                cur["status"] = "closed"
            ts = row.get("timestamp")
            if ts is not None and (cur.get("timestamp") is None or ts > cur["timestamp"]):
                cur["timestamp"] = ts

    markets = list(by_cid.values())
    for m in markets:
        if not m.get("has_pnl"):
            m["pnl"] = None
        else:
            m["pnl"] = _round_pnl_to_cents(float(m["pnl"]))
        m.pop("has_pnl", None)

    markets.sort(
        key=lambda m: (
            -(m["timestamp"] or 0),
            -(abs(m["pnl"]) if m.get("pnl") is not None else -1),
        )
    )

    total_pnl = sum(float(m["pnl"]) for m in markets if m.get("pnl") is not None)
    return {
        "wallet": wallet,
        "date": date,
        "count": len(markets),
        "total_pnl": _round_pnl_to_cents(total_pnl) if markets else 0.0,
        "markets": markets[:limit],
    }


async def fetch_wallet_activity(
    address: str,
    *,
    date: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    wallet = normalize_wallet(address)
    limit = max(1, min(int(limit), 1000))

    params: dict[str, Any] = {"user": wallet, "limit": min(500, limit)}
    start_ms = end_ms = None
    if date:
        start_ms, end_ms = _et_day_bounds(date)
        params["start"] = start_ms // 1000
        params["end"] = end_ms // 1000

    rows: list[dict[str, Any]] = []
    offset = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=8.0)) as client:
        while len(rows) < limit:
            page_limit = min(500, limit - len(rows))
            page_params = {**params, "limit": page_limit, "offset": offset}
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
                if start_ms is not None and item["timestamp"] < start_ms:
                    continue
                if end_ms is not None and item["timestamp"] >= end_ms:
                    continue
                rows.append(item)
            if len(batch) < page_limit:
                break
            offset += len(batch)
            if offset >= 5000:
                break

        pnl_by_condition: dict[str, float] = {}
        if date:
            closed = await _paginate_closed_positions(
                client, wallet, date=date, limit=200
            )
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

    markets = _group_activity_by_market(rows[:limit], pnl_by_condition=pnl_by_condition)

    return {
        "wallet": wallet,
        "date": date,
        "count": len(rows),
        "name": name,
        "activity": rows[:limit],
        "markets": markets,
    }
