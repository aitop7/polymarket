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
    """BTC Up/Down 5m realized-PnL series + headline for the selected interval.

    Built from closed-position settles (not the all-market user-pnl API).
    """
    wallet = normalize_wallet(address)
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

    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=8.0)) as client:
        # Pull closed BTC rows (newest first), then rebuild chronological cumulative curve.
        closed: list[dict[str, Any]] = []
        offset = 0
        exhausted = False
        while len(closed) < max_btc and offset <= 200_000:
            page = 50
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
                exhausted = True
                break
            if not isinstance(batch, list) or not batch:
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
            if len(batch) < page:
                exhausted = True
                break
            offset += len(batch)

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
        bucket_ms = 5 * 60_000  # 5m aligns with market windows
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
    end_pnl = points[-1]["pnl"] if points else None

    # Age from first BTC settle.
    age_days = None
    if points:
        age_days = max(0.0, (now_ms - int(points[0]["t"])) / 86_400_000.0)

    use_absolute = key in {"all", "max"} or _period_too_short(key, age_days)
    if end_pnl is None:
        headline = None
    elif use_absolute or start_pnl is None or len(points) < 2:
        headline = end_pnl
    else:
        headline = _round_pnl_to_cents(float(end_pnl) - float(start_pnl))

    if headline is not None:
        headline = _round_pnl_to_cents(float(headline))

    return {
        "wallet": wallet,
        "interval": key,
        "fidelity": fidelity,
        "scope": "btc_updown_5m",
        "start_pnl": start_pnl,
        "end_pnl": end_pnl,
        "pnl": headline,
        "absolute": use_absolute,
        "account_age_days": age_days,
        "has_more": (not exhausted) and len(closed) >= max_btc,
        "n_positions": len(closed),
        "series": points,
    }


def _settle_day(pos: dict[str, Any]) -> str | None:
    """Prefer closed-position endDate (ET calendar day); fall back to timestamp."""
    end_date = pos.get("end_date")
    if isinstance(end_date, str) and len(end_date) >= 10:
        return end_date[:10]
    ts = pos.get("timestamp")
    if ts is not None:
        return datetime.fromtimestamp(int(ts) / 1000, tz=_ET).strftime("%Y-%m-%d")
    return None


async def _aggregate_btc_closed_by_day(
    client: httpx.AsyncClient,
    wallet: str,
    *,
    max_btc: int = 3000,
    max_offset: int = 200_000,
    before: str | None = None,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """
    Sum realized BTC Up/Down 5m PnL by settle day.

    Trades API history is shallow for hyperactive wallets, so closed-positions
    is the durable source for “every traded day”.
    """
    max_btc = max(50, min(int(max_btc), 50_000))
    by_day: dict[str, dict[str, Any]] = {}
    offset = 0
    n_btc = 0
    exhausted = False

    while n_btc < max_btc and offset <= max_offset:
        page = 50
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
            exhausted = True
            break
        if not isinstance(batch, list) or not batch:
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
                    "n_positions": 1,
                    "t": int(ts) if ts else 0,
                }
            else:
                bucket["pnl"] = float(bucket["pnl"]) + float(pnl or 0.0)
                bucket["n_positions"] = int(bucket["n_positions"]) + 1
                if ts and int(ts) > int(bucket.get("t") or 0):
                    bucket["t"] = int(ts)
            if n_btc >= max_btc:
                break

        if n_btc >= max_btc:
            break
        if len(batch) < page:
            exhausted = True
            break
        offset += len(batch)

    has_more = (not exhausted) and n_btc >= max_btc
    for bucket in by_day.values():
        bucket["pnl"] = _round_pnl_to_cents(float(bucket["pnl"]))
    return by_day, has_more


async def fetch_wallet_daily_pnl(
    address: str,
    *,
    days: int = 90,
    scan_limit: int = 3000,
    before: str | None = None,
) -> dict[str, Any]:
    """BTC Up/Down 5m realized PnL by settle day (newest first)."""
    wallet = normalize_wallet(address)
    days = max(1, min(int(days), 730))
    scan_limit = max(50, min(int(scan_limit), 50_000))
    if before:
        try:
            datetime.strptime(before, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("before must be YYYY-MM-DD") from exc

    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=8.0)) as client:
        by_day, has_more = await _aggregate_btc_closed_by_day(
            client, wallet, max_btc=scan_limit, before=before
        )

    daily = sorted(by_day.values(), key=lambda r: r["date"], reverse=True)
    # Keep at most `days` rows for this page.
    daily = daily[:days]
    for row in daily:
        row["cum_pnl"] = None  # BTC-only day sums; not account-level cumulative

    return {
        "wallet": wallet,
        "days": days,
        "scan_limit": scan_limit,
        "before": before,
        "traded_days": len(by_day),
        "has_more": has_more or len(by_day) > len(daily),
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
                if isinstance(end_date, str) and len(end_date) >= 10:
                    end_date = end_date[:10]
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
) -> dict[str, Any]:
    """Per-market PnL from closed positions, merged with same-day activity markets."""
    wallet = normalize_wallet(address)
    limit = max(1, min(int(limit), 500))
    act_limit = max(limit, min(int(activity_limit or max(limit * 4, 500)), 1000))

    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=8.0)) as client:
        closed = await _paginate_closed_positions(
            client, wallet, date=date, limit=max(limit, 500) if date else limit
        )
        open_rows: list[dict[str, Any]] = []
        activity_markets: list[dict[str, Any]] = []
        if date:
            if isinstance(activity_payload, dict) and activity_payload.get("markets") is not None:
                activity_markets = list(activity_payload.get("markets") or [])
            else:
                # Activity is the complete “traded this day” set; closed PnL alone misses
                # in-progress / not-yet-indexed settles and was empty in shallow caches.
                act = await fetch_wallet_activity(
                    address, date=date, limit=act_limit, _client=client
                )
                activity_markets = list(act.get("markets") or [])
        elif include_open:
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

    by_cid: dict[str, dict[str, Any]] = {}
    for row in closed + open_rows:
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

    clipped = markets[:limit]
    total_pnl = sum(float(m["pnl"]) for m in clipped if m.get("pnl") is not None)
    return {
        "wallet": wallet,
        "date": date,
        "count": len(clipped),
        "total_count": len(markets),
        "has_more": len(markets) > len(clipped),
        "total_pnl": _round_pnl_to_cents(total_pnl) if clipped else 0.0,
        "markets": clipped,
    }


async def fetch_wallet_activity(
    address: str,
    *,
    date: str | None = None,
    limit: int = 200,
    offset: int = 0,
    _client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    wallet = normalize_wallet(address)
    limit = max(1, min(int(limit), 1000))
    offset = max(0, min(int(offset), 20_000))

    params: dict[str, Any] = {"user": wallet, "limit": min(500, limit)}
    start_ms = end_ms = None
    if date:
        start_ms, end_ms = _et_day_bounds(date)
        params["start"] = start_ms // 1000
        params["end"] = end_ms // 1000

    rows: list[dict[str, Any]] = []
    page_offset = offset
    owns_client = _client is None

    async def _run(client: httpx.AsyncClient) -> dict[str, Any]:
        nonlocal page_offset
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
                if start_ms is not None and item["timestamp"] < start_ms:
                    continue
                if end_ms is not None and item["timestamp"] >= end_ms:
                    continue
                rows.append(item)
            if len(batch) < page_limit:
                break
            page_offset += len(batch)
            if page_offset >= offset + 5000:
                break

        pnl_by_condition: dict[str, float] = {}
        if date:
            closed = await _paginate_closed_positions(
                client, wallet, date=date, limit=500
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

        clipped = rows[:limit]
        markets = _group_activity_by_market(clipped, pnl_by_condition=pnl_by_condition)
        # Use the raw Data API offset we actually consumed. BTC filtering means
        # page_offset >> len(clipped); returning len(clipped) made "Fetch more"
        # re-read the same window and miss REDEEM / older fills.
        has_more = len(rows) >= limit
        return {
            "wallet": wallet,
            "date": date,
            "count": len(clipped),
            "offset": offset,
            "next_offset": page_offset if page_offset > offset else offset + len(clipped),
            "api_offset": page_offset,
            "has_more": has_more,
            "name": name,
            "activity": clipped,
            "markets": markets,
        }

    if owns_client:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=8.0)) as client:
            return await _run(client)
    assert _client is not None
    return await _run(_client)
