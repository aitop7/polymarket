"""Historical market replay engine."""

from __future__ import annotations

from typing import Any, AsyncIterator

import pandas as pd

from app.core.data import FEATURE_COLUMNS, load_market_frame
from app.engine.portfolio import Portfolio
from app.strategies.registry import create_strategy
from strategies.base import Action, MarketEndContext, OrderIntent, Side, TickContext


def _row_features(row: pd.Series) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in FEATURE_COLUMNS:
        if c in row.index:
            v = row[c]
            out[c] = None if pd.isna(v) else float(v)
    return out


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

    return TickContext(
        market_id=str(market_id),
        timestamp=int(row["timestamp"]),
        btc_price=f("btc_price"),
        btc_open=f("btc_open_price"),
        up_price=float(f("up_price", 0.5) or 0.5),
        down_price=float(f("down_price", 0.5) or 0.5),
        elapsed_seconds=elapsed,
        remaining_seconds=remaining,
        winner=int(row["winner"]) if "winner" in row.index and pd.notna(row["winner"]) else None,
        features=_row_features(row),
        portfolio=portfolio.snapshot(),
        row_index=idx,
    )


def run_market_backtest(
    market_id: str,
    *,
    split: str | None = None,
    strategy_name: str = "lgbm_edge",
    strategy_params: dict[str, Any] | None = None,
    starting_cash: float = 1000.0,
) -> dict[str, Any]:
    df = load_market_frame(market_id, split=split)
    portfolio = Portfolio(cash=starting_cash)
    strategy = create_strategy(strategy_name, strategy_params)
    if strategy is not None and hasattr(strategy, "reset"):
        strategy.reset()

    equity: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []

    for i, row in df.iterrows():
        ctx = _tick_from_row(row, market_id=market_id, idx=int(i), portfolio=portfolio)
        intents: list[OrderIntent] = []
        if strategy is not None:
            intents = strategy.on_tick(ctx) or []
        for intent in intents:
            fill = portfolio.apply_intent(
                intent,
                market_id=str(market_id),
                timestamp=ctx.timestamp,
                up_price=ctx.up_price,
                down_price=ctx.down_price,
                source="strategy",
            )
            if fill:
                signals.append({**fill.to_dict(), "model_p_up": ctx.model_p_up})
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
    return {
        "market_id": str(market_id),
        "winner": winner,
        "starting_cash": starting_cash,
        "ending_cash": final_equity,
        "pnl": final_equity - starting_cash,
        "payout": payout,
        "n_fills": len([f for f in portfolio.fills if f.action != "SETTLE"]),
        "fills": [f.to_dict() for f in portfolio.fills],
        "signals": signals,
        "equity": equity[:: max(1, len(equity) // 100)] if equity else [],
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

    def step(self) -> dict[str, Any] | None:
        if self.done or self.index >= len(self.df):
            if not self.done:
                self._finish()
            return None

        row = self.df.iloc[self.index]
        ctx = _tick_from_row(row, market_id=self.market_id, idx=self.index, portfolio=self.portfolio)
        fills_out: list[dict[str, Any]] = []

        # Manual orders first
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
            for intent in self.strategy.on_tick(ctx) or []:
                fill = self.portfolio.apply_intent(
                    intent,
                    market_id=self.market_id,
                    timestamp=ctx.timestamp,
                    up_price=ctx.up_price,
                    down_price=ctx.down_price,
                    source="strategy",
                )
                if fill:
                    fills_out.append({**fill.to_dict(), "model_p_up": ctx.model_p_up})

        tick = {
            "type": "tick",
            "market_id": self.market_id,
            "index": self.index,
            "total": len(self.df),
            "timestamp": ctx.timestamp,
            "btc_price": ctx.btc_price,
            "btc_open": ctx.btc_open,
            "up_price": ctx.up_price,
            "down_price": ctx.down_price,
            "elapsed_seconds": ctx.elapsed_seconds,
            "remaining_seconds": ctx.remaining_seconds,
            "model_p_up": ctx.model_p_up,
            "portfolio": self.portfolio.snapshot().to_dict(),
            "equity": self.portfolio.mark_to_market(ctx.up_price, ctx.down_price),
            "fills": fills_out,
            "winner": ctx.winner,
        }
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

        interval = 1.0 / self.speed
        while not self.done:
            if self.paused:
                await asyncio.sleep(0.05)
                continue
            msg = self.step()
            if msg is None:
                break
            yield msg
            await asyncio.sleep(interval)
