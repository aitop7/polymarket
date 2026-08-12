"""Historical market replay engine."""

from __future__ import annotations

from typing import Any, AsyncIterator

import numpy as np
import pandas as pd

from app.core.data import FEATURE_COLUMNS, load_market_frame
from app.core.pricing import quotes_from_row
from app.engine.portfolio import Portfolio
from app.strategies.registry import create_strategy
from strategies.base import MarketEndContext, OrderIntent, TickContext
from strategies.safe_pair import is_matched_buy_pair, parse_edge_from_reason


def _row_features(row: pd.Series) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in FEATURE_COLUMNS:
        if c in row.index:
            v = row[c]
            out[c] = None if pd.isna(v) else float(v)
    return out


_ASK_NEAR_BANDS = ("0_1", "1_3")


def _near_ask_depth(row: pd.Series, prefix: str) -> float | None:
    total = 0.0
    found = False
    for band in _ASK_NEAR_BANDS:
        col = f"{prefix}_ask_{band}"
        if col in row.index and pd.notna(row[col]):
            total += float(row[col])
            found = True
    return total if found else None


def _tick_from_row(row: pd.Series, *, market_id: str, idx: int, portfolio: Portfolio) -> TickContext:
    def f(col: str, default: float | None = None) -> float | None:
        if col not in row.index or pd.isna(row[col]):
            return default
        return float(row[col])

    elapsed = f("elapsed_seconds", 0.0) or 0.0
    remaining = f("remaining_seconds", 0.0) or 0.0
    if "start_time" in row.index and "timestamp" in row.index:
        start = float(row["start_time"])
        end = float(row["end_time"]) if "end_time" in row.index else start + 300_000
        ts = float(row["timestamp"])
        elapsed = (ts - start) / 1000.0
        remaining = (end - ts) / 1000.0

    q = quotes_from_row(row)
    return TickContext(
        market_id=str(market_id),
        timestamp=int(row["timestamp"]),
        btc_price=f("btc_price"),
        btc_open=f("btc_open_price"),
        up_price=q["up_price"],
        down_price=q["down_price"],
        elapsed_seconds=elapsed,
        remaining_seconds=remaining,
        winner=int(row["winner"]) if "winner" in row.index and pd.notna(row["winner"]) else None,
        features=_row_features(row),
        portfolio=portfolio.snapshot(),
        row_index=idx,
        up_ask_price=f("up_ask_price"),
        down_ask_price=f("down_ask_price"),
        up_ask_shares=f("up_ask_shares"),
        down_ask_shares=f("down_ask_shares"),
        up_bid_price=f("up_bid_price"),
        down_bid_price=f("down_bid_price"),
        up_bid_shares=f("up_bid_shares"),
        down_bid_shares=f("down_bid_shares"),
        up_ask_near_depth=_near_ask_depth(row, "up"),
        down_ask_near_depth=_near_ask_depth(row, "down"),
    )


def _apply_strategy_intents(
    portfolio: Portfolio,
    intents: list[OrderIntent],
    *,
    market_id: str,
    timestamp: int,
    up_price: float,
    down_price: float,
    model_p_up: float | None = None,
) -> list[dict[str, Any]]:
    fills = portfolio.apply_intents(
        intents,
        market_id=market_id,
        timestamp=timestamp,
        up_price=up_price,
        down_price=down_price,
        source="strategy",
        atomic_pair=is_matched_buy_pair(intents),
    )
    return [{**f.to_dict(), "model_p_up": model_p_up} for f in fills]


def run_market_backtest(
    market_id: str,
    *,
    split: str | None = None,
    strategy_name: str = "lgbm_edge",
    strategy_params: dict[str, Any] | None = None,
    starting_cash: float = 1000.0,
    portfolio: Portfolio | None = None,
    strategy: Any = None,
) -> dict[str, Any]:
    df = load_market_frame(market_id, split=split)
    if portfolio is None:
        portfolio = Portfolio(cash=starting_cash)
        market_start_cash = starting_cash
    else:
        market_start_cash = portfolio.cash

    if strategy is None:
        strategy = create_strategy(strategy_name, strategy_params)
        if strategy is not None and hasattr(strategy, "reset"):
            strategy.reset()

    equity: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    opportunities_found = 0
    fill_start_idx = len(portfolio.fills)

    for i, row in df.iterrows():
        ctx = _tick_from_row(row, market_id=market_id, idx=int(i), portfolio=portfolio)
        if strategy is not None and hasattr(strategy, "opportunity_at_tick"):
            ok, _, _ = strategy.opportunity_at_tick(ctx)
            if ok:
                opportunities_found += 1
        intents: list[OrderIntent] = []
        if strategy is not None:
            intents = strategy.on_tick(ctx) or []
        if intents:
            signals.extend(
                _apply_strategy_intents(
                    portfolio,
                    intents,
                    market_id=str(market_id),
                    timestamp=ctx.timestamp,
                    up_price=ctx.up_price,
                    down_price=ctx.down_price,
                    model_p_up=ctx.model_p_up,
                )
            )
        equity.append(
            {
                "t": ctx.timestamp,
                "equity": portfolio.mark_to_market(ctx.up_price, ctx.down_price),
                "cash": portfolio.cash,
            }
        )

    winner = int(df.iloc[-1]["winner"]) if "winner" in df.columns else 0
    last_ts = int(df.iloc[-1]["timestamp"])
    payout = portfolio.settle(winner, market_id=str(market_id), timestamp=last_ts)
    if strategy is not None:
        strategy.on_market_end(
            MarketEndContext(
                market_id=str(market_id),
                winner=winner,
                portfolio=portfolio.snapshot(),
                trades=[f.to_dict() for f in portfolio.fills],
            )
        )

    final_equity = portfolio.cash
    market_pnl = final_equity - market_start_cash
    market_fills = portfolio.fills[fill_start_idx:]
    trade_fills = [f for f in market_fills if f.action != "SETTLE"]
    pairs_filled = len([f for f in trade_fills if f.side == "UP" and f.action == "BUY"])
    net_edges: list[float] = []
    for f in trade_fills:
        if f.action == "BUY":
            _, net = parse_edge_from_reason(f.reason)
            if net is not None:
                net_edges.append(net)

    return {
        "market_id": str(market_id),
        "winner": winner,
        "starting_cash": market_start_cash,
        "ending_cash": final_equity,
        "pnl": market_pnl,
        "payout": payout,
        "n_fills": len(trade_fills),
        "fills": [f.to_dict() for f in market_fills],
        "signals": signals,
        "equity": equity[:: max(1, len(equity) // 100)] if equity else [],
        "stats": {
            "opportunities_found": opportunities_found,
            "markets_with_opportunities": 1 if opportunities_found > 0 else 0,
            "pairs_filled": pairs_filled,
            "avg_net_edge": sum(net_edges) / len(net_edges) if net_edges else None,
        },
    }


class ReplaySession:
    """Stateful replay for monitor / paper trading over WebSocket."""

    def __init__(
        self,
        market_id: str,
        *,
        split: str | None = None,
        strategy_name: str | None = None,
        strategy_params: dict[str, Any] | None = None,
        starting_cash: float = 1000.0,
        speed: float = 10.0,
    ) -> None:
        self.market_id = str(market_id)
        self.df = load_market_frame(market_id, split=split)
        self.portfolio = Portfolio(cash=starting_cash)
        self.strategy = create_strategy(strategy_name or "none", strategy_params)
        if self.strategy is not None and hasattr(self.strategy, "reset"):
            self.strategy.reset()
        self.speed = max(0.1, float(speed))
        self.index = 0
        self.done = False
        self.paused = False
        self.pending_manual: list[OrderIntent] = []

    def queue_manual(self, intent: OrderIntent) -> None:
        self.pending_manual.append(intent)

    def current_row(self) -> pd.Series | None:
        if self.index >= len(self.df):
            return None
        return self.df.iloc[self.index]

    def seek_to(self, timestamp_ms: int) -> None:
        """Jump playhead to the first row at/after timestamp (view scrub; skips fills)."""
        if self.df.empty or "timestamp" not in self.df.columns:
            return
        ts = self.df["timestamp"].to_numpy(dtype="int64", copy=False)
        idx = int(np.searchsorted(ts, int(timestamp_ms), side="left"))
        self.index = max(0, min(idx, len(self.df) - 1))
        self.done = False

    def _build_tick(self, *, apply_orders: bool) -> dict[str, Any] | None:
        if self.index >= len(self.df):
            return None
        row = self.df.iloc[self.index]
        ctx = _tick_from_row(row, market_id=self.market_id, idx=self.index, portfolio=self.portfolio)
        fills_out: list[dict[str, Any]] = []

        if apply_orders:
            while self.pending_manual:
                intent = self.pending_manual.pop(0)
                fill = self.portfolio.apply_intent(
                    intent,
                    market_id=self.market_id,
                    timestamp=ctx.timestamp,
                    up_price=ctx.up_price,
                    down_price=ctx.down_price,
                    source="manual",
                )
                if fill:
                    fills_out.append(fill.to_dict())

            if self.strategy is not None:
                intents = self.strategy.on_tick(ctx) or []
                if intents:
                    fills_out.extend(
                        _apply_strategy_intents(
                            self.portfolio,
                            intents,
                            market_id=self.market_id,
                            timestamp=ctx.timestamp,
                            up_price=ctx.up_price,
                            down_price=ctx.down_price,
                            model_p_up=ctx.model_p_up,
                        )
                    )

        return {
            "type": "tick",
            "market_id": self.market_id,
            "index": self.index,
            "total": len(self.df),
            "timestamp": ctx.timestamp,
            "btc_price": ctx.btc_price,
            "btc_open": ctx.btc_open,
            "btc_twap_30s": (
                float(row["btc_twap_30s"])
                if "btc_twap_30s" in row.index and pd.notna(row["btc_twap_30s"])
                else (
                    float(row["btc_price"])
                    if "btc_price" in row.index and pd.notna(row["btc_price"])
                    else None
                )
            ),
            "btc_chainlink": float(row["btc_chainlink"])
            if "btc_chainlink" in row.index and pd.notna(row["btc_chainlink"])
            else None,
            "up_price": ctx.up_price,
            "down_price": ctx.down_price,
            "up_buy": ctx.up_price,
            "down_buy": ctx.down_price,
            "up_sell": max(1e-6, ctx.up_price - 0.01),
            "down_sell": max(1e-6, ctx.down_price - 0.01),
            "elapsed_seconds": ctx.elapsed_seconds,
            "remaining_seconds": ctx.remaining_seconds,
            "model_p_up": ctx.model_p_up,
            "portfolio": self.portfolio.snapshot().to_dict(),
            "equity": self.portfolio.mark_to_market(
                max(1e-6, ctx.up_price - 0.01),
                max(1e-6, ctx.down_price - 0.01),
            ),
            "fills": fills_out,
            "winner": ctx.winner,
        }

    def peek_tick(self) -> dict[str, Any] | None:
        """Emit tick at current index without advancing (used after seek)."""
        return self._build_tick(apply_orders=False)

    def step(self) -> dict[str, Any] | None:
        if self.done or self.index >= len(self.df):
            if not self.done:
                self._finish()
            return None

        tick = self._build_tick(apply_orders=True)
        if tick is None:
            return None
        self.index += 1
        if self.index >= len(self.df):
            settle = self._finish()
            tick["settlement"] = settle
            tick["type"] = "tick_end"
        return tick

    def _finish(self) -> dict[str, Any]:
        self.done = True
        winner = int(self.df.iloc[-1]["winner"]) if "winner" in self.df.columns else 0
        last_ts = int(self.df.iloc[-1]["timestamp"])
        payout = self.portfolio.settle(winner, market_id=self.market_id, timestamp=last_ts)
        if self.strategy is not None:
            self.strategy.on_market_end(
                MarketEndContext(
                    market_id=self.market_id,
                    winner=winner,
                    portfolio=self.portfolio.snapshot(),
                    trades=[f.to_dict() for f in self.portfolio.fills],
                )
            )
        return {
            "winner": winner,
            "payout": payout,
            "portfolio": self.portfolio.snapshot().to_dict(),
            "ending_cash": self.portfolio.cash,
        }

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        import asyncio

        while not self.done:
            if self.paused:
                await asyncio.sleep(0.05)
                continue
            msg = self.step()
            if msg is None:
                break
            yield msg
            # Re-read speed each tick so WS speed updates apply immediately
            await asyncio.sleep(1.0 / max(0.1, float(self.speed)))
