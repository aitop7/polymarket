"""Virtual portfolio and fill simulation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strategies.base import Action, OrderIntent, PortfolioSnapshot, Side
from strategies.fees import buy_cash_required


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

    def _buy_price(self, intent: OrderIntent, up_price: float, down_price: float) -> float | None:
        if intent.limit_price is not None:
            price = float(intent.limit_price)
        else:
            price = up_price if intent.side == Side.UP else down_price
        if price <= 0 or price >= 1:
            return None
        return price

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
        if intent.action == Action.BUY:
            price = self._buy_price(intent, up_price, down_price)
            if price is None:
                return None
            fee_rate = max(0.0, float(getattr(intent, "fee_rate", 0.0) or 0.0))
            fee_model = str(getattr(intent, "fee_model", "polymarket") or "polymarket")
            if intent.shares is not None and intent.shares > 0:
                shares = float(intent.shares)
                cost = buy_cash_required(shares, price, fee_rate=fee_rate, fee_model=fee_model)
            elif intent.size_usd is not None and intent.size_usd > 0:
                # Approximate: size_usd targets notional before fee
                budget = min(float(intent.size_usd), float(self.cash))
                if budget <= 0:
                    return None
                if fee_model == "polymarket" and fee_rate > 0:
                    denom = price * (1.0 + fee_rate * (1.0 - price))
                elif fee_model == "flat" and fee_rate > 0:
                    denom = price * (1.0 + fee_rate)
                else:
                    denom = price
                shares = budget / denom if denom > 0 else 0.0
                cost = buy_cash_required(shares, price, fee_rate=fee_rate, fee_model=fee_model)
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
        if intent.side == Side.UP:
            price = up_sell if up_sell is not None else max(1e-6, up_price - 0.01)
        else:
            price = down_sell if down_sell is not None else max(1e-6, down_price - 0.01)
        if intent.limit_price is not None:
            price = float(intent.limit_price)
        if price <= 0 or price >= 1:
            return None

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

    def apply_intents(
        self,
        intents: list[OrderIntent],
        *,
        market_id: str,
        timestamp: int,
        up_price: float,
        down_price: float,
        up_sell: float | None = None,
        down_sell: float | None = None,
        source: str = "strategy",
        atomic_pair: bool = False,
    ) -> list[Fill]:
        """Apply intents; when atomic_pair, both legs fill or neither (rollback)."""
        if not intents:
            return []
        if not atomic_pair:
            out: list[Fill] = []
            for intent in intents:
                fill = self.apply_intent(
                    intent,
                    market_id=market_id,
                    timestamp=timestamp,
                    up_price=up_price,
                    down_price=down_price,
                    up_sell=up_sell,
                    down_sell=down_sell,
                    source=source,
                )
                if fill:
                    out.append(fill)
            return out

        snap_cash = self.cash
        snap_up = self.up_shares
        snap_down = self.down_shares
        snap_n_fills = len(self.fills)
        fills: list[Fill] = []
        for intent in intents:
            fill = self.apply_intent(
                intent,
                market_id=market_id,
                timestamp=timestamp,
                up_price=up_price,
                down_price=down_price,
                up_sell=up_sell,
                down_sell=down_sell,
                source=source,
            )
            if fill is None:
                # rollback
                self.cash = snap_cash
                self.up_shares = snap_up
                self.down_shares = snap_down
                del self.fills[snap_n_fills:]
                return []
            fills.append(fill)
        return fills

    def settle(self, winner: int, *, market_id: str, timestamp: int) -> float:
        """Settle binary shares: winning side pays $1, losing $0."""
        up_pay = 1.0 if int(winner) == 1 else 0.0
        down_pay = 1.0 if int(winner) == 0 else 0.0
        payout = self.up_shares * up_pay + self.down_shares * down_pay
        self.cash += payout
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
