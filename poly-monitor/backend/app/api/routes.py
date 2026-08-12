"""REST + WebSocket API."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.data import (
    ALL_SPLITS,
    SPLITS,
    book_at,
    list_markets,
    load_market_frame,
    market_summary,
    series_for_chart,
)
from app.core.live_dataset import TWAP_SPLIT
from app.core.market_index import (
    build_market_index,
    filter_history_markets,
    find_market_at,
    find_market_by_date_time,
    list_dates,
    list_markets_for_date,
)
from app.backtest.runner import run_backtest
from app.engine.replay import ReplaySession
from app.strategies.registry import list_strategies
from strategies.base import Action, OrderIntent, Side

router = APIRouter()

# In-memory paper sessions
_PAPER: dict[str, ReplaySession] = {}


class BacktestRequest(BaseModel):
    strategy: str = "lgbm_edge"
    split: str = "validation"
    market_ids: list[str] | None = None
    limit: int = Field(default=20, ge=1, le=500)
    date: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    starting_cash: float = 1000.0


class PaperSessionRequest(BaseModel):
    market_id: str
    split: str | None = None
    strategy: str = "none"
    params: dict[str, Any] = Field(default_factory=dict)
    starting_cash: float = 1000.0
    speed: float = 10.0


class PaperOrderRequest(BaseModel):
    session_id: str
    side: str  # UP | DOWN
    action: str = "BUY"
    size_usd: float | None = 10.0
    shares: float | None = None


@router.get("/health")
def health() -> dict[str, Any]:
    from app.core.live_dataset import data_health_thresholds

    return {
        "ok": True,
        "fetch_real_root": str(settings.fetch_real_root),
        "training_exists": settings.training_dir.is_dir(),
        "features_exists": settings.features_dir.is_dir(),
        "data_health_thresholds": data_health_thresholds(),
    }


@router.get("/strategies")
def get_strategies() -> list[dict[str, Any]]:
    return list_strategies()


@router.get("/markets/dates")
def get_market_dates(
    split: str = Query("validation", pattern="^(train|validation|test|twap)$"),
    rebuild_index: bool = Query(False),
) -> dict[str, Any]:
    if rebuild_index:
        build_market_index(split, force=True)
    dates = list_dates(split)
    return {
        "split": split,
        "count": len(dates),
        "dates": dates,
        "min": dates[0] if dates else None,
        "max": dates[-1] if dates else None,
    }


@router.get("/markets/at")
def get_market_at(
    split: str = Query("validation", pattern="^(train|validation|test|twap)$"),
    date: str | None = Query(None, description="YYYY-MM-DD in ET"),
    time: str | None = Query(None, description="HH:MM in ET"),
    t: int | None = Query(None, description="unix ms"),
) -> dict[str, Any]:
    if date and time:
        m = find_market_by_date_time(split, date, time)
    elif t is not None:
        m = find_market_at(split, int(t))
    elif date:
        day = list_markets_for_date(split, date)
        m = day[0] if day else None
    else:
        raise HTTPException(400, "Provide date+time, date, or t")
    if not m:
        raise HTTPException(404, "No market found for selection")
    return m


@router.get("/markets")
def get_markets(
    split: str = Query("validation", pattern="^(train|validation|test|twap)$"),
    limit: int = Query(50, ge=1, le=5000),
    date: str | None = Query(None, description="Filter by ET date YYYY-MM-DD"),
    rebuild_index: bool = Query(False),
) -> dict[str, Any]:
    if rebuild_index:
        build_market_index(split, force=True)
    if date:
        markets = list_markets_for_date(split, date)
    else:
        # Prefer indexed full list (fast); fall back to legacy limited scan
        try:
            markets = filter_history_markets(split, build_market_index(split))
            if limit is not None:
                markets = markets[: max(0, int(limit))]
        except Exception:
            markets = list_markets(split, limit=limit)
    return {"split": split, "date": date, "count": len(markets), "markets": markets}


async def _ensure_twap_history(market_id: str, split: str | None) -> None:
    """If selecting a TWAP history market, repair local gaps from VPS when needed."""
    from app.core.live_dataset import find_live_market_dir
    from app.live.vps_sync import get_vps_sync

    mid = str(market_id).strip()
    if not mid:
        return
    if split != TWAP_SPLIT and find_live_market_dir(mid) is None:
        return
    await get_vps_sync().ensure_history_market(mid)


@router.get("/markets/{market_id}")
async def get_market(market_id: str, split: str | None = None) -> dict[str, Any]:
    await _ensure_twap_history(market_id, split)
    meta = market_summary(market_id, split=split)
    if not meta:
        raise HTTPException(404, f"Market {market_id} not found")
    df = load_market_frame(market_id, split=meta["split"])
    from app.core.pricing import quotes_from_row

    first_q = quotes_from_row(df.iloc[0])
    last_q = quotes_from_row(df.iloc[-1])
    return {
        **meta,
        "series": series_for_chart(df, market_id=str(market_id)),
        "first": {
            "timestamp": int(df.iloc[0]["timestamp"]),
            "btc_price": float(df.iloc[0]["btc_price"]) if "btc_price" in df.columns else None,
            "up_price": first_q["up_price"],
            "down_price": first_q["down_price"],
            "up_sell": first_q["up_sell"],
            "down_sell": first_q["down_sell"],
        },
        "last": {
            "timestamp": int(df.iloc[-1]["timestamp"]),
            "btc_price": float(df.iloc[-1]["btc_price"]) if "btc_price" in df.columns else None,
            "up_price": last_q["up_price"],
            "down_price": last_q["down_price"],
            "up_sell": last_q["up_sell"],
            "down_sell": last_q["down_sell"],
        },
    }


@router.post("/markets/{market_id}/health/recheck")
async def recheck_market_health(market_id: str) -> dict[str, Any]:
    """Force VPS re-pull + rewrite meta.data_health / gap comments."""
    from app.live.vps_sync import get_vps_sync

    mid = str(market_id or "").strip()
    if not mid:
        raise HTTPException(400, "market_id required")
    result = await get_vps_sync().recheck_history_market(mid)
    if not result.get("ok"):
        raise HTTPException(404, str(result.get("error") or "recheck failed"))
    return result


@router.post("/markets/{market_id}/repair")
async def repair_market(market_id: str) -> dict[str, Any]:
    """Fill missed trades on the VPS, re-pull the archive, then restamp health."""
    from app.live.vps_sync import get_vps_sync

    mid = str(market_id or "").strip()
    if not mid:
        raise HTTPException(400, "market_id required")
    result = await get_vps_sync().repair_history_market(mid)
    if not result.get("ok"):
        status = 409 if "still live" in str(result.get("error") or "") else 502
        raise HTTPException(status, str(result.get("error") or "repair failed"))
    return result


@router.get("/markets/{market_id}/book")
async def get_book(
    market_id: str, t: int | None = None, split: str | None = None
) -> dict[str, Any]:
    await _ensure_twap_history(market_id, split)
    meta = market_summary(market_id, split=split)
    if not meta:
        raise HTTPException(404, f"Market {market_id} not found")
    df = load_market_frame(market_id, split=meta["split"])
    return book_at(df, t)


@router.get("/markets/{market_id}/holders")
async def get_market_holders(
    market_id: str, limit: int = Query(20, ge=1, le=20)
) -> dict[str, Any]:
    """Top Up/Down holders for a history (or any) TWAP market via Data API."""
    from app.core.market_social import market_holders

    return await market_holders(market_id, limit=limit)


@router.get("/markets/{market_id}/traders")
def get_market_traders(
    market_id: str, limit: int = Query(20, ge=1, le=50)
) -> dict[str, Any]:
    """Top earners (realized PnL) and top volume wallets from trades.parquet."""
    from app.core.trader_stats import market_traders

    return market_traders(market_id, limit=limit)


@router.get("/markets/{market_id}/traders/{wallet}")
def get_market_trader_detail(market_id: str, wallet: str) -> dict[str, Any]:
    """Per-wallet PnL / volume breakdown + fill tape for history detail view."""
    from app.core.trader_stats import trader_detail

    detail = trader_detail(market_id, wallet)
    if detail is None:
        raise HTTPException(404, f"Trader {wallet} not found for market {market_id}")
    return detail


@router.get("/markets/{market_id}/activity")
async def get_market_activity(
    market_id: str, limit: int = Query(1500, ge=1, le=2000)
) -> dict[str, Any]:
    """Trade tape for a history market (paginated Data API; full 5m window)."""
    from app.core.market_social import market_activity

    return await market_activity(market_id, limit=limit)


@router.get("/wallets/{address}")
async def get_wallet_summary(address: str) -> dict[str, Any]:
    """Wallet profile summary (positions value, biggest win, explorer links)."""
    from app.core.wallet_activity import fetch_wallet_summary, normalize_wallet

    try:
        normalize_wallet(address)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        return await fetch_wallet_summary(address)
    except Exception as exc:
        raise HTTPException(502, f"Wallet lookup failed: {exc}") from exc


@router.get("/wallets/{address}/pnl")
async def get_wallet_pnl(
    address: str,
    interval: str = Query("1d", pattern="^(1d|1w|1m|1y|ytd|all|max)$"),
) -> dict[str, Any]:
    """PnL timeseries for 1D / 1W / 1M / 1Y / YTD / ALL (Polymarket user-pnl API)."""
    from app.core.wallet_activity import fetch_wallet_pnl

    try:
        return await fetch_wallet_pnl(address, interval=interval)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Wallet PnL failed: {exc}") from exc


@router.get("/wallets/{address}/daily")
async def get_wallet_daily_pnl(
    address: str,
    days: int = Query(90, ge=1, le=730),
) -> dict[str, Any]:
    """Per-day PnL deltas (newest first)."""
    from app.core.wallet_activity import fetch_wallet_daily_pnl

    try:
        return await fetch_wallet_daily_pnl(address, days=days)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Wallet daily PnL failed: {exc}") from exc


@router.get("/wallets/{address}/activity")
async def get_wallet_activity(
    address: str,
    date: str | None = Query(None, description="ET calendar day YYYY-MM-DD"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """Wallet activity tape (optional date filter). Links to Polygonscan + Orbscan."""
    from app.core.wallet_activity import fetch_wallet_activity

    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(400, "date must be YYYY-MM-DD") from exc
    try:
        return await fetch_wallet_activity(address, date=date, limit=limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Wallet activity failed: {exc}") from exc


@router.get("/wallets/{address}/markets")
async def get_wallet_markets(
    address: str,
    date: str | None = Query(None, description="ET calendar day YYYY-MM-DD"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """Per-market PnL (closed positions for date, or closed+open overall)."""
    from app.core.wallet_activity import fetch_wallet_markets

    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(400, "date must be YYYY-MM-DD") from exc
    try:
        return await fetch_wallet_markets(address, date=date, limit=limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Wallet markets failed: {exc}") from exc


@router.get("/markets/{market_id}/neighbors")
def get_neighbors(market_id: str, split: str | None = None) -> dict[str, Any]:
    meta = market_summary(market_id, split=split)
    if not meta:
        raise HTTPException(404, f"Market {market_id} not found")
    from pathlib import Path

    if meta["split"] == TWAP_SPLIT:
        # Same history filter as the picker — no Next into the live window.
        idx = filter_history_markets(TWAP_SPLIT, build_market_index(TWAP_SPLIT))
        ids = [str(r["market_id"]) for r in idx]
    else:
        d = settings.features_dir / meta["split"]
        if not d.is_dir():
            d = settings.training_dir / meta["split"]
        ids = sorted(p.stem for p in d.glob("*.parquet"))
    try:
        i = ids.index(str(market_id))
    except ValueError:
        return {"prev": None, "next": None, "split": meta["split"]}
    return {
        "split": meta["split"],
        "prev": ids[i - 1] if i > 0 else None,
        "next": ids[i + 1] if i + 1 < len(ids) else None,
        "index": i,
        "total": len(ids),
    }


@router.post("/backtest")
def post_backtest(body: BacktestRequest) -> dict[str, Any]:
    if body.split not in ALL_SPLITS:
        raise HTTPException(400, "Invalid split")
    try:
        return run_backtest(
            strategy=body.strategy,
            split=body.split,
            market_ids=body.market_ids,
            limit=body.limit,
            date=body.date,
            strategy_params=body.params,
            starting_cash=body.starting_cash,
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/backtest/market")
def post_backtest_market(body: BacktestRequest) -> dict[str, Any]:
    """Run one market and return full fills + equity (for detail pane)."""
    if body.split not in ALL_SPLITS:
        raise HTTPException(400, "Invalid split")
    mid = None
    if body.market_ids:
        mid = str(body.market_ids[0])
    if not mid:
        raise HTTPException(400, "market_ids[0] required")
    try:
        from app.engine.replay import run_market_backtest

        return run_market_backtest(
            mid,
            split=body.split,
            strategy_name=body.strategy,
            strategy_params=body.params,
            starting_cash=body.starting_cash,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/paper/session")
def create_paper_session(body: PaperSessionRequest) -> dict[str, Any]:
    meta = market_summary(body.market_id, split=body.split)
    if not meta:
        raise HTTPException(404, f"Market {body.market_id} not found")
    sid = str(uuid.uuid4())
    session = ReplaySession(
        body.market_id,
        split=meta["split"],
        strategy_name=body.strategy,
        strategy_params=body.params,
        starting_cash=body.starting_cash,
        speed=body.speed,
    )
    _PAPER[sid] = session
    return {
        "session_id": sid,
        "market_id": body.market_id,
        "split": meta["split"],
        "rows": len(session.df),
        "starting_cash": body.starting_cash,
        "strategy": body.strategy,
        "speed": body.speed,
    }


@router.post("/paper/order")
def paper_order(body: PaperOrderRequest) -> dict[str, Any]:
    session = _PAPER.get(body.session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    try:
        side = Side(body.side.upper())
        action = Action(body.action.upper())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    intent = OrderIntent(side=side, action=action, size_usd=body.size_usd, shares=body.shares, reason="manual")
    session.queue_manual(intent)
    return {"ok": True, "queued": True}


@router.get("/paper/{session_id}")
def paper_status(session_id: str) -> dict[str, Any]:
    session = _PAPER.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return {
        "session_id": session_id,
        "market_id": session.market_id,
        "index": session.index,
        "total": len(session.df),
        "done": session.done,
        "paused": session.paused,
        "portfolio": session.portfolio.snapshot().to_dict(),
        "fills": [f.to_dict() for f in session.portfolio.fills[-50:]],
    }


@router.get("/live/state")
async def live_state() -> dict[str, Any]:
    """One-shot live market snapshot (BTC + Up/Down + CLOB ladder)."""
    from app.live import get_live_service

    return await get_live_service().snapshot()


@router.get("/live/series")
async def live_series(
    market_id: str | None = None,
    lookback_ms: int = 300_000,
) -> dict[str, Any]:
    """Historical chart points for the active (or requested) live market window."""
    from app.live import get_live_service
    from app.live.vps_sync import get_vps_sync

    svc = get_live_service()
    # Ensure market discovery so window bounds / id are current.
    await svc.market_meta()
    mid = str(market_id or svc._market_id or "") or None
    if mid:
        # Live reload/seed: pull this market's history from VPS so charts continue.
        await get_vps_sync().ensure_active_market(mid, force=True)
    return svc.series(market_id or mid, lookback_ms=lookback_ms)


@router.get("/live/holders")
async def live_holders(limit: int = 20) -> dict[str, Any]:
    """Top Up/Down holders for the active live market."""
    from app.live import get_live_service

    return await get_live_service().holders(limit=limit)


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    """Stream live market state. View-only; no order placement.

    Client may send `{interval_s: 0.5}` on connect and later
    `{type: "interval", interval_s: 0.1..2}` to change poll rate.
    """
    from app.live import get_live_service

    await websocket.accept()
    svc = get_live_service()
    last_market_id: str | None = None
    interval_s = 0.5

    def _clamp_interval(raw: Any) -> float:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return interval_s
        return max(0.1, min(2.0, v))

    try:
        # First client message sets poll interval (default 0.5s).
        try:
            init = await asyncio.wait_for(websocket.receive_json(), timeout=2.0)
            if isinstance(init, dict) and "interval_s" in init:
                interval_s = _clamp_interval(init.get("interval_s"))
        except (asyncio.TimeoutError, WebSocketDisconnect):
            pass

        meta = await svc.market_meta()
        if meta:
            last_market_id = meta.get("market_id")
            await websocket.send_json(meta)

        async def reader() -> None:
            nonlocal interval_s
            while True:
                msg = await websocket.receive_json()
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "interval" or "interval_s" in msg:
                    interval_s = _clamp_interval(msg.get("interval_s"))

        reader_task = asyncio.create_task(reader())
        send_lock = asyncio.Lock()

        async def send_json(payload: dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_json(payload)

        async def holders_sender() -> None:
            while True:
                try:
                    payload = await svc.holders(limit=20)
                    await send_json({"type": "holders", **payload})
                except WebSocketDisconnect:
                    raise
                except Exception:
                    # Don't kill the holders loop on a single Data API failure.
                    pass
                await asyncio.sleep(0.25)

        async def activity_sender() -> None:
            while True:
                try:
                    trades = svc.drain_activity(limit=40)
                    if trades:
                        await send_json({"type": "activity", "trades": trades})
                except WebSocketDisconnect:
                    raise
                except Exception:
                    pass
                await asyncio.sleep(0.12)

        async def series_sender() -> None:
            """Push chart backfill (prices + volume buckets) over WS — no HTTP poll."""
            from app.live.vps_sync import get_vps_sync

            # Immediate seed once the socket is up.
            first = True
            while True:
                try:
                    mid = str(svc._market_id or "") or None
                    if mid:
                        # Occasional VPS catch-up for parquet (throttled inside client).
                        await get_vps_sync().ensure_active_market(mid, force=first)
                    payload = svc.series(mid, lookback_ms=360_000)
                    await send_json({"type": "series", **payload})
                    first = False
                except WebSocketDisconnect:
                    raise
                except Exception:
                    pass
                await asyncio.sleep(8.0)

        holders_task = asyncio.create_task(holders_sender())
        activity_task = asyncio.create_task(activity_sender())
        series_task = asyncio.create_task(series_sender())
        try:
            last_start: int | None = None
            while True:
                t0 = time.perf_counter()
                snap = await svc.snapshot()
                mid = snap.get("market_id")
                start = snap.get("start_time")
                rolled = (mid and mid != last_market_id) or (
                    start is not None and start != last_start and last_start is not None
                )
                if rolled or (last_market_id is None and mid):
                    await send_json(
                        {
                            "type": "market",
                            "live": True,
                            "market_id": mid,
                            "slug": snap.get("slug"),
                            "start_time": start,
                            "end_time": snap.get("end_time"),
                            "price_to_beat": snap.get("price_to_beat"),
                        }
                    )
                    # Clear client lists immediately on roll; next holders tick refills.
                    await send_json(
                        {
                            "type": "holders",
                            "live": True,
                            "market_id": mid,
                            "condition_id": None,
                            "updated_at": int(time.time() * 1000),
                            "up": [],
                            "down": [],
                        }
                    )
                    # Fresh chart seed for the new window (WS, not HTTP).
                    try:
                        payload = svc.series(str(mid) if mid else None, lookback_ms=360_000)
                        await send_json({"type": "series", **payload})
                    except Exception:
                        pass
                    last_market_id = mid
                    last_start = start if start is not None else last_start
                snap["interval_s"] = interval_s
                await send_json(snap)
                # Target cadence = interval_s (don't add fetch time on top).
                elapsed = time.perf_counter() - t0
                await asyncio.sleep(max(0.0, interval_s - elapsed))
        finally:
            holders_task.cancel()
            activity_task.cancel()
            series_task.cancel()
            reader_task.cancel()
            for task in (holders_task, activity_task, series_task, reader_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    except WebSocketDisconnect:
        return
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass


@router.websocket("/ws/replay")
async def ws_replay(websocket: WebSocket) -> None:
    await websocket.accept()
    session: ReplaySession | None = None
    try:
        init = await websocket.receive_json()
        market_id = str(init.get("market_id") or "")
        if not market_id:
            await websocket.send_json({"type": "error", "message": "market_id required"})
            await websocket.close()
            return
        split = init.get("split")
        strategy = init.get("strategy") or "none"
        params = init.get("params") or {}
        speed = float(init.get("speed") or 20.0)
        starting_cash = float(init.get("starting_cash") or settings.default_cash)
        session_id = init.get("session_id")

        if session_id and session_id in _PAPER:
            session = _PAPER[session_id]
            session.speed = speed
        else:
            meta = market_summary(market_id, split=split)
            if not meta:
                await websocket.send_json({"type": "error", "message": f"market {market_id} not found"})
                await websocket.close()
                return
            session = ReplaySession(
                market_id,
                split=meta["split"],
                strategy_name=strategy,
                strategy_params=params,
                starting_cash=starting_cash,
                speed=speed,
            )
            try:
                start_ts = int(init["start_timestamp"]) if init.get("start_timestamp") is not None else None
            except (TypeError, ValueError):
                start_ts = None
            if start_ts is not None:
                session.seek_to(start_ts)
            if init.get("paper"):
                sid = str(uuid.uuid4())
                _PAPER[sid] = session
                await websocket.send_json({"type": "session", "session_id": sid, "market": meta})
            else:
                await websocket.send_json({"type": "session", "session_id": None, "market": meta})

        async def reader() -> None:
            nonlocal session
            assert session is not None
            while True:
                msg = await websocket.receive_json()
                typ = msg.get("type")
                if typ == "pause":
                    session.paused = True
                elif typ == "resume":
                    session.paused = False
                elif typ == "speed":
                    session.speed = max(0.1, float(msg.get("speed") or session.speed))
                elif typ == "seek":
                    try:
                        ts = int(msg.get("timestamp") or 0)
                    except (TypeError, ValueError):
                        continue
                    session.seek_to(ts)
                    session.paused = True
                    tick = session.peek_tick()
                    if tick is not None:
                        tick["seek"] = True
                        await websocket.send_json(tick)
                elif typ == "order":
                    side = Side(str(msg.get("side", "UP")).upper())
                    action = Action(str(msg.get("action", "BUY")).upper())
                    intent = OrderIntent(
                        side=side,
                        action=action,
                        size_usd=msg.get("size_usd", 10.0),
                        shares=msg.get("shares"),
                        reason="manual",
                    )
                    session.queue_manual(intent)

        reader_task = asyncio.create_task(reader())
        try:
            assert session is not None
            async for tick in session.stream():
                await websocket.send_json(tick)
            await websocket.send_json(
                {
                    "type": "done",
                    "portfolio": session.portfolio.snapshot().to_dict(),
                    "fills": [f.to_dict() for f in session.portfolio.fills],
                }
            )
        finally:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
    except WebSocketDisconnect:
        return
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
