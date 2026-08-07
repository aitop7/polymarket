"""Virtual portfolio and fill simulation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strategies.base import Action, OrderIntent, PortfolioSnapshot, Side


@dataclass
class Fill:
    timestamp: int
    market_id: str
    side: str
    action: str
    price: float
    shares: float
    cost: float
    reason: str = ""
    source: str = "strategy"  # strategy | manual

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "market_id": self.market_id,
            "side": self.side,
            "action": self.action,
            "price": self.price,
            "shares": self.shares,
            "cost": self.cost,
            "reason": self.reason,
            "source": self.source,
        }


@dataclass
class Portfolio:
    cash: float
    up_shares: float = 0.0
    down_shares: float = 0.0
    realized_pnl: float = 0.0
    fills: list[Fill] = field(default_factory=list)

    def snapshot(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            cash=self.cash,
            up_shares=self.up_shares,
            down_shares=self.down_shares,
            realized_pnl=self.realized_pnl,
        )

    def mark_to_market(self, up_price: float, down_price: float) -> float:
        return self.cash + self.up_shares * up_price + self.down_shares * down_price

    def apply_intent(
        self,
        intent: OrderIntent,
        *,
        market_id: str,
        timestamp: int,
        up_price: float,
        down_price: float,
        up_sell: float | None = None,
        down_sell: float | None = None,
        source: str = "strategy",
    ) -> Fill | None:
        # Buy at displayed buy price; sell 1¢ lower unless explicit sell quotes provided
        if intent.action == Action.BUY:
            price = up_price if intent.side == Side.UP else down_price
        else:
            if intent.side == Side.UP:
                price = up_sell if up_sell is not None else max(1e-6, up_price - 0.01)
            else:
                price = down_sell if down_sell is not None else max(1e-6, down_price - 0.01)
        if price <= 0 or price >= 1:
            return None

        if intent.action == Action.BUY:
            if intent.shares is not None and intent.shares > 0:
                shares = float(intent.shares)
                cost = shares * price
            elif intent.size_usd is not None and intent.size_usd > 0:
                cost = min(float(intent.size_usd), self.cash)
                if cost <= 0:
                    return None
                shares = cost / price
            else:
                return None
            if cost > self.cash + 1e-9:
                return None
            self.cash -= cost
            if intent.side == Side.UP:
                self.up_shares += shares
            else:
                self.down_shares += shares
            fill = Fill(
                timestamp=timestamp,
                market_id=market_id,
                side=intent.side.value,
                action="BUY",
                price=price,
                shares=shares,
                cost=cost,
                reason=intent.reason,
                source=source,
            )
            self.fills.append(fill)
            return fill

        # SELL
        held = self.up_shares if intent.side == Side.UP else self.down_shares
        shares = float(intent.shares) if intent.shares is not None else held
        shares = min(shares, held)
        if shares <= 0:
            return None
        proceeds = shares * price
        self.cash += proceeds
        if intent.side == Side.UP:
            self.up_shares -= shares
        else:
            self.down_shares -= shares
        fill = Fill(
            timestamp=timestamp,
            market_id=market_id,
            side=intent.side.value,
            action="SELL",
            price=price,
            shares=shares,
            cost=-proceeds,
            reason=intent.reason,
            source=source,
        )
        self.fills.append(fill)
        return fill

    def settle(self, winner: int, *, market_id: str, timestamp: int) -> float:
        """Settle binary shares: winning side pays $1, losing $0."""
        up_pay = 1.0 if int(winner) == 1 else 0.0
        down_pay = 1.0 if int(winner) == 0 else 0.0
        payout = self.up_shares * up_pay + self.down_shares * down_pay
        cost_basis_est = 0.0  # tracked via cash already
        self.cash += payout
        pnl_delta = payout  # shares were paid from cash already; this is redemption
        self.realized_pnl += payout - (
            sum(f.cost for f in self.fills if f.market_id == market_id and f.action == "BUY")
            - sum(-f.cost for f in self.fills if f.market_id == market_id and f.action == "SELL")
        )
        self.up_shares = 0.0
        self.down_shares = 0.0
        self.fills.append(
            Fill(
                timestamp=timestamp,
                market_id=market_id,
                side="SETTLE",
                action="SETTLE",
                price=1.0 if winner == 1 else 0.0,
                shares=0.0,
                cost=-payout,
                reason=f"winner={'UP' if winner == 1 else 'DOWN'}",
                source="settle",
            )
        )
        return payout
