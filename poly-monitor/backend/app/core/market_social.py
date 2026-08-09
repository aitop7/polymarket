"""Holders + trade activity for a market (live or history) via Polymarket Data API."""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.live_dataset import find_live_market_dir, _read_meta

DATA_API_URL = "https://data-api.polymarket.com"


def _meta_for_market(market_id: str) -> dict[str, Any] | None:
    d = find_live_market_dir(str(market_id))
    if d is None:
        return None
    return _read_meta(d / "meta.json")


def _norm_holder(h: dict[str, Any]) -> dict[str, Any]:
    wallet = str(h.get("proxyWallet") or "")
    name = str(h.get("name") or "").strip()
    pseudo = str(h.get("pseudonym") or "").strip()
    public = bool(h.get("displayUsernamePublic"))
    if public and name:
        display = name
    elif pseudo:
        display = pseudo
    elif name:
        display = name
    elif wallet:
        display = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet
    else:
        display = "—"
    amount = h.get("amount")
    try:
        shares = float(amount) if amount is not None else 0.0
    except (TypeError, ValueError):
        shares = 0.0
    return {
        "proxy_wallet": wallet,
        "display_name": display,
        "amount": shares,
        "profile_image": str(
            h.get("profileImageOptimized") or h.get("profileImage") or ""
        ),
        "verified": bool(h.get("verified")),
        "outcome_index": h.get("outcomeIndex"),
    }


def _trade_row(
    trade: dict[str, Any],
    *,
    token_up: str | None,
    token_down: str | None,
    start_ms: int | None,
    end_ms: int | None,
) -> dict[str, Any] | None:
    tx = str(trade.get("transactionHash") or trade.get("transaction_hash") or "")
    try:
        ts = int(trade.get("timestamp") or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts < 10_000_000_000:
        ts *= 1000
    # Allow post-window settlement prints (Data API often lands after end_time).
    if end_ms and ts >= int(end_ms) + 30 * 60_000:
        return None
    if start_ms and ts < int(start_ms) - 120_000:
        return None

    asset = str(trade.get("asset") or trade.get("asset_id") or "")
    is_down: bool | None = None
    if token_up and asset == str(token_up):
        is_down = False
    elif token_down and asset == str(token_down):
        is_down = True
    else:
        outcome = str(trade.get("outcome") or "").strip().lower()
        if outcome in {"up", "yes"}:
            is_down = False
        elif outcome in {"down", "no"}:
            is_down = True
        else:
            try:
                is_down = int(trade.get("outcomeIndex")) == 1
            except (TypeError, ValueError):
                return None
    if is_down is None:
        return None

    side_raw = str(trade.get("side") or "BUY").upper()
    is_sell = side_raw in {"SELL", "S"}
    try:
        price = float(trade.get("price") or 0)
        size = float(trade.get("size") or 0)
    except (TypeError, ValueError):
        return None
    if size <= 0 or price < 0:
        return None

    wallet = str(
        trade.get("proxyWallet")
        or trade.get("proxy_wallet")
        or trade.get("wallet")
        or ""
    )
    name = str(trade.get("name") or trade.get("pseudonym") or "").strip()
    if not name and wallet:
        name = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet
    if not name:
        name = "Trader"
    return {
        "id": tx or f"{ts}:{wallet}:{asset}:{price}:{size}",
        "timestamp": ts,
        "name": name,
        "pseudonym": str(trade.get("pseudonym") or "") or None,
        "proxy_wallet": wallet,
        "profile_image": str(trade.get("profileImage") or trade.get("profile_image") or "")
        or None,
        "outcome": "Down" if is_down else "Up",
        "side": "SELL" if is_sell else "BUY",
        "price": price,
        "shares": float(size),
        "usd": round(price * float(size), 2),
        "transaction_hash": tx or None,
        "token": bool(is_down),
        "is_sell": bool(is_sell),
    }


async def market_holders(market_id: str, *, limit: int = 20) -> dict[str, Any]:
    meta = _meta_for_market(market_id) or {}
    cid = str(meta.get("condition_id") or "").strip()
    token_up = str(meta.get("up_token_id") or "").strip() or None
    token_down = str(meta.get("down_token_id") or "").strip() or None
    now_ms = int(time.time() * 1000)
    empty = {
        "market_id": str(market_id),
        "condition_id": cid or None,
        "updated_at": now_ms,
        "live": False,
        "up": [],
        "down": [],
    }
    if not cid:
        return empty

    try:
        async with httpx.AsyncClient(
            base_url=DATA_API_URL,
            timeout=httpx.Timeout(12.0, connect=5.0),
        ) as http:
            resp = await http.get(
                "/holders",
                params={
                    "market": cid,
                    "limit": max(1, min(20, int(limit))),
                    "minBalance": 1,
                },
            )
            resp.raise_for_status()
            blocks = resp.json()
    except Exception:
        return empty

    if not isinstance(blocks, list):
        return empty

    up: list[dict[str, Any]] = []
    down: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        token = str(block.get("token") or "")
        holders = [_norm_holder(h) for h in (block.get("holders") or []) if isinstance(h, dict)]
        holders.sort(key=lambda x: x["amount"], reverse=True)
        if token_up and token == token_up:
            up = holders
        elif token_down and token == token_down:
            down = holders
        else:
            idxs = {
                h.get("outcomeIndex")
                for h in (block.get("holders") or [])
                if isinstance(h, dict) and h.get("outcomeIndex") is not None
            }
            if idxs == {0} or (0 in idxs and 1 not in idxs and not up):
                up = holders
            elif idxs == {1} or (1 in idxs and not down):
                down = holders

    return {
        "market_id": str(market_id),
        "condition_id": cid,
        "updated_at": now_ms,
        "live": False,
        "up": up,
        "down": down,
    }


async def market_activity(market_id: str, *, limit: int = 1500) -> dict[str, Any]:
    """Full-window trade tape for history playback (Data API is newest-first, paginated)."""
    meta = _meta_for_market(market_id) or {}
    cid = str(meta.get("condition_id") or "").strip()
    token_up = str(meta.get("up_token_id") or "").strip() or None
    token_down = str(meta.get("down_token_id") or "").strip() or None
    try:
        start_ms = int(meta["start_time"]) if meta.get("start_time") is not None else None
    except (TypeError, ValueError):
        start_ms = None
    try:
        end_ms = int(meta["end_time"]) if meta.get("end_time") is not None else None
    except (TypeError, ValueError):
        end_ms = None

    want = max(1, min(2000, int(limit)))
    page_size = 100
    # Busy 5m markets often have 800–1200 prints; paginate past the newest cluster.
    max_pages = 20
    trades: list[dict[str, Any]] = []
    seen: set[str] = set()
    if cid:
        try:
            async with httpx.AsyncClient(
                base_url=DATA_API_URL,
                timeout=httpx.Timeout(20.0, connect=5.0),
            ) as http:
                offset = 0
                for _ in range(max_pages):
                    resp = await http.get(
                        "/trades",
                        params={
                            "market": cid,
                            "limit": page_size,
                            "offset": offset,
                        },
                    )
                    resp.raise_for_status()
                    raw = resp.json()
                    rows = raw if isinstance(raw, list) else []
                    if not rows:
                        break

                    oldest_raw: int | None = None
                    for item in rows:
                        if not isinstance(item, dict):
                            continue
                        try:
                            raw_ts = int(item.get("timestamp") or 0)
                        except (TypeError, ValueError):
                            raw_ts = 0
                        if raw_ts > 0:
                            if raw_ts < 10_000_000_000:
                                raw_ts *= 1000
                            oldest_raw = (
                                raw_ts
                                if oldest_raw is None
                                else min(oldest_raw, raw_ts)
                            )
                        row = _trade_row(
                            item,
                            token_up=token_up,
                            token_down=token_down,
                            start_ms=start_ms,
                            end_ms=end_ms,
                        )
                        if row is None:
                            continue
                        tid = str(row.get("id") or "")
                        if tid and tid in seen:
                            continue
                        if tid:
                            seen.add(tid)
                        trades.append(row)

                    # Reached before market open (plus lead-in allowance) — stop paging.
                    if start_ms is not None and oldest_raw is not None:
                        if oldest_raw < int(start_ms) - 120_000:
                            break
                    if len(rows) < page_size:
                        break
                    if len(trades) >= want:
                        break
                    offset += page_size
        except Exception:
            pass

    trades.sort(key=lambda t: int(t.get("timestamp") or 0), reverse=True)
    return {
        "market_id": str(market_id),
        "condition_id": cid or None,
        "trades": trades[:want],
    }
