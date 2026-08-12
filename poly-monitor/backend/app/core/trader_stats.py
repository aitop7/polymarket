"""Per-wallet PnL / volume leaderboards from local trades.parquet."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.live_dataset import find_live_market_dir
from app.live.fetch_live_series import fetch_live_market_dir


@dataclass
class _WalletAgg:
    cash: float = 0.0
    up_pos: float = 0.0
    down_pos: float = 0.0
    volume_usd: float = 0.0
    fills: int = 0
    buy_usd: float = 0.0
    sell_usd: float = 0.0
    buy_fills: int = 0
    sell_fills: int = 0
    up_buy_shares: float = 0.0
    up_sell_shares: float = 0.0
    down_buy_shares: float = 0.0
    down_sell_shares: float = 0.0


def _market_dir(market_id: str) -> Path | None:
    mid = str(market_id).strip()
    if not mid:
        return None
    return find_live_market_dir(mid) or fetch_live_market_dir(mid)


def _read_meta(market_dir: Path) -> dict[str, Any]:
    path = market_dir / "meta.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _winner_up(meta: dict[str, Any]) -> bool | None:
    """True = Up won, False = Down won, None = unresolved."""
    winner = meta.get("winner")
    if winner is True or winner == 1 or winner == "1" or winner == "Up":
        return True
    if winner is False or winner == 0 or winner == "0" or winner == "Down":
        return False
    return None


def _row_flags(row: Any, *, use_new: bool) -> tuple[bool, bool] | None:
    """Return (is_up, is_buy) or None if unusable."""
    if use_new:
        return bool(getattr(row, "is_up", False)), bool(getattr(row, "is_buy", False))
    # Legacy: token False=UP True=DOWN; side False=BUY True=SELL
    token = bool(getattr(row, "token", False))
    side = bool(getattr(row, "side", False))
    return (not token), (not side)


def _winner_label(winner_up: bool | None) -> str | None:
    if winner_up is True:
        return "Up"
    if winner_up is False:
        return "Down"
    return None


def _load_trades_df(market_dir: Path) -> pd.DataFrame | None:
    trades_path = market_dir / "trades.parquet"
    if not trades_path.is_file():
        return None
    try:
        df = pd.read_parquet(trades_path)
    except Exception:
        return None
    if df.empty or "timestamp" not in df.columns or "shares" not in df.columns:
        return None
    if "wallet" not in df.columns:
        return None
    return df


def _apply_fill(agg: _WalletAgg, *, is_up: bool, is_buy: bool, price: float, shares: float) -> None:
    notional = price * shares
    agg.volume_usd += notional
    agg.fills += 1
    if is_buy:
        agg.cash -= notional
        agg.buy_usd += notional
        agg.buy_fills += 1
        if is_up:
            agg.up_pos += shares
            agg.up_buy_shares += shares
        else:
            agg.down_pos += shares
            agg.down_buy_shares += shares
    else:
        agg.cash += notional
        agg.sell_usd += notional
        agg.sell_fills += 1
        if is_up:
            agg.up_pos -= shares
            agg.up_sell_shares += shares
        else:
            agg.down_pos -= shares
            agg.down_sell_shares += shares


def _row_from_agg(
    wallet: str, agg: _WalletAgg, *, winner_up: bool | None
) -> dict[str, Any]:
    pnl = float(agg.cash)
    if winner_up is True:
        pnl += float(agg.up_pos) * 1.0
    elif winner_up is False:
        pnl += float(agg.down_pos) * 1.0
    return {
        "wallet": wallet,
        "pnl": round(pnl, 4),
        "volume_usd": round(float(agg.volume_usd), 4),
        "fills": int(agg.fills),
        "buy_usd": round(float(agg.buy_usd), 4),
        "sell_usd": round(float(agg.sell_usd), 4),
        "buy_fills": int(agg.buy_fills),
        "sell_fills": int(agg.sell_fills),
        "up_buy_shares": round(float(agg.up_buy_shares), 2),
        "up_sell_shares": round(float(agg.up_sell_shares), 2),
        "down_buy_shares": round(float(agg.down_buy_shares), 2),
        "down_sell_shares": round(float(agg.down_sell_shares), 2),
        "up_pos": round(float(agg.up_pos), 2),
        "down_pos": round(float(agg.down_pos), 2),
    }


def compute_trader_stats(
    market_id: str, *, limit: int = 20
) -> dict[str, Any]:
    """
    Rank wallets by realized PnL for one market window.

    PnL: cash from buys/sells + redemption of leftover inventory at $1/$0 when
    meta.winner is known. Unresolved: cash only (no redemption).
    Volume: sum(price * shares) over that wallet's fills.
    """
    mid = str(market_id).strip()
    limit = max(1, min(int(limit), 50))
    empty = {
        "market_id": mid,
        "resolved": False,
        "winner": None,
        "by_pnl": [],
        "by_volume": [],
    }
    market_dir = _market_dir(mid)
    if market_dir is None:
        return empty

    meta = _read_meta(market_dir)
    winner_up = _winner_up(meta)
    resolved = winner_up is not None
    winner = _winner_label(winner_up)

    df = _load_trades_df(market_dir)
    if df is None:
        return {**empty, "resolved": resolved, "winner": winner}

    use_new = "is_up" in df.columns and "is_buy" in df.columns
    aggs: dict[str, _WalletAgg] = {}
    for row in df.itertuples(index=False):
        wallet = str(getattr(row, "wallet", "") or "").strip().lower()
        if not wallet:
            continue
        try:
            shares = float(getattr(row, "shares") or 0.0)
            price = float(getattr(row, "price") or 0.0)
        except (TypeError, ValueError):
            continue
        if shares <= 0 or price < 0:
            continue
        flags = _row_flags(row, use_new=use_new)
        if flags is None:
            continue
        is_up, is_buy = flags
        agg = aggs.get(wallet)
        if agg is None:
            agg = _WalletAgg()
            aggs[wallet] = agg
        _apply_fill(agg, is_up=is_up, is_buy=is_buy, price=price, shares=shares)

    rows = [
        _row_from_agg(wallet, agg, winner_up=winner_up) for wallet, agg in aggs.items()
    ]
    by_pnl = sorted(rows, key=lambda r: (-float(r["pnl"]), -float(r["volume_usd"]), r["wallet"]))[
        :limit
    ]
    by_volume = sorted(
        rows, key=lambda r: (-float(r["volume_usd"]), -float(r["pnl"]), r["wallet"])
    )[:limit]

    return {
        "market_id": mid,
        "resolved": resolved,
        "winner": winner,
        "by_pnl": by_pnl,
        "by_volume": by_volume,
    }


def trader_detail(market_id: str, wallet: str) -> dict[str, Any] | None:
    """Full stats + fill tape for one wallet in a market."""
    mid = str(market_id).strip()
    w = str(wallet or "").strip().lower()
    if not mid or not w:
        return None
    market_dir = _market_dir(mid)
    if market_dir is None:
        return None

    meta = _read_meta(market_dir)
    winner_up = _winner_up(meta)
    resolved = winner_up is not None
    winner = _winner_label(winner_up)

    df = _load_trades_df(market_dir)
    if df is None:
        return {
            "market_id": mid,
            "wallet": w,
            "resolved": resolved,
            "winner": winner,
            **_row_from_agg(w, _WalletAgg(), winner_up=winner_up),
            "fills_list": [],
        }

    use_new = "is_up" in df.columns and "is_buy" in df.columns
    has_tx = "transaction_hash" in df.columns
    agg = _WalletAgg()
    fills_list: list[dict[str, Any]] = []
    for row in df.itertuples(index=False):
        row_w = str(getattr(row, "wallet", "") or "").strip().lower()
        if row_w != w:
            continue
        try:
            ts = int(getattr(row, "timestamp") or 0)
            shares = float(getattr(row, "shares") or 0.0)
            price = float(getattr(row, "price") or 0.0)
        except (TypeError, ValueError):
            continue
        if shares <= 0 or price < 0 or ts <= 0:
            continue
        flags = _row_flags(row, use_new=use_new)
        if flags is None:
            continue
        is_up, is_buy = flags
        _apply_fill(agg, is_up=is_up, is_buy=is_buy, price=price, shares=shares)
        tx = str(getattr(row, "transaction_hash", "") or "") if has_tx else ""
        fills_list.append(
            {
                "timestamp": ts,
                "is_up": bool(is_up),
                "is_buy": bool(is_buy),
                "price": float(price),
                "shares": round(float(shares), 2),
                "usd": round(float(price) * float(shares), 4),
                "transaction_hash": tx or None,
            }
        )

    fills_list.sort(key=lambda f: int(f["timestamp"]))
    return {
        "market_id": mid,
        "wallet": w,
        "resolved": resolved,
        "winner": winner,
        **_row_from_agg(w, agg, winner_up=winner_up),
        "fills_list": fills_list,
    }


def market_traders(market_id: str, *, limit: int = 20) -> dict[str, Any]:
    return compute_trader_stats(market_id, limit=limit)
