"""REST + WebSocket API."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.data import SPLITS, book_at, list_markets, load_market_frame, market_summary, series_for_chart
from app.core.market_index import (
    build_market_index,
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
    return {
        "ok": True,
        "fetch_real_root": str(settings.fetch_real_root),
        "training_exists": settings.training_dir.is_dir(),
        "features_exists": settings.features_dir.is_dir(),
    }


@router.get("/strategies")
def get_strategies() -> list[dict[str, Any]]:
    return list_strategies()


@router.get("/markets/dates")
def get_market_dates(
    split: str = Query("validation", pattern="^(train|validation|test)$"),
) -> dict[str, Any]:
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
    split: str = Query("validation", pattern="^(train|validation|test)$"),
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
    split: str = Query("validation", pattern="^(train|validation|test)$"),
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
            markets = build_market_index(split)
            if limit is not None:
                markets = markets[: max(0, int(limit))]
        except Exception:
            markets = list_markets(split, limit=limit)
    return {"split": split, "date": date, "count": len(markets), "markets": markets}


@router.get("/markets/{market_id}")
def get_market(market_id: str, split: str | None = None) -> dict[str, Any]:
    meta = market_summary(market_id, split=split)
    if not meta:
        raise HTTPException(404, f"Market {market_id} not found")
    df = load_market_frame(market_id, split=meta["split"])
    from app.core.pricing import quotes_from_row

    first_q = quotes_from_row(df.iloc[0])
    last_q = quotes_from_row(df.iloc[-1])
    return {
        **meta,
        "series": series_for_chart(df),
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


@router.get("/markets/{market_id}/book")
def get_book(market_id: str, t: int | None = None, split: str | None = None) -> dict[str, Any]:
    meta = market_summary(market_id, split=split)
    if not meta:
        raise HTTPException(404, f"Market {market_id} not found")
    df = load_market_frame(market_id, split=meta["split"])
    return book_at(df, t)


@router.get("/markets/{market_id}/neighbors")
def get_neighbors(market_id: str, split: str | None = None) -> dict[str, Any]:
    meta = market_summary(market_id, split=split)
    if not meta:
        raise HTTPException(404, f"Market {market_id} not found")
    # Lightweight id list
    from pathlib import Path

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
    if body.split not in SPLITS:
        raise HTTPException(400, "Invalid split")
    try:
        return run_backtest(
            strategy=body.strategy,
            split=body.split,
            market_ids=body.market_ids,
            limit=body.limit,
            strategy_params=body.params,
            starting_cash=body.starting_cash,
        )
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
        try:
            while True:
                t0 = time.perf_counter()
                snap = await svc.snapshot()
                mid = snap.get("market_id")
                if mid and mid != last_market_id:
                    await websocket.send_json(
                        {
                            "type": "market",
                            "live": True,
                            "market_id": mid,
                            "slug": snap.get("slug"),
                            "start_time": snap.get("start_time"),
                            "end_time": snap.get("end_time"),
                        }
                    )
                    last_market_id = mid
                snap["interval_s"] = interval_s
                await websocket.send_json(snap)
                # Target cadence = interval_s (don't add fetch time on top).
                elapsed = time.perf_counter() - t0
                await asyncio.sleep(max(0.0, interval_s - elapsed))
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
