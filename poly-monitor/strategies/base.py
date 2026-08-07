"""Strategy plugin contracts for poly-monitor."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class Side(str, Enum):
    UP = "UP"
    DOWN = "DOWN"


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class OrderIntent:
    side: Side
    action: Action
    size_usd: float | None = None
    shares: float | None = None
    limit_price: float | None = None
    reason: str = ""


@dataclass
class PortfolioSnapshot:
    cash: float
    up_shares: float
    down_shares: float
    realized_pnl: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "cash": self.cash,
            "up_shares": self.up_shares,
            "down_shares": self.down_shares,
            "realized_pnl": self.realized_pnl,
        }


@dataclass
class TickContext:
    market_id: str
    timestamp: int
    btc_price: float | None
    btc_open: float | None
    up_price: float
    down_price: float
    elapsed_seconds: float
    remaining_seconds: float
    winner: int | None
    features: dict[str, Any] = field(default_factory=dict)
    model_p_up: float | None = None
    portfolio: PortfolioSnapshot | None = None
    row_index: int = 0


@dataclass
class MarketEndContext:
    market_id: str
    winner: int  # 1=UP, 0=DOWN
    portfolio: PortfolioSnapshot
    trades: list[dict[str, Any]]


class Strategy(Protocol):
    name: str

    def on_tick(self, ctx: TickContext) -> list[OrderIntent]: ...

    def on_market_end(self, ctx: MarketEndContext) -> None: ...
