"""Dual resting-limit strategy: BUY UP@A + DOWN@B, one-sided exit at A'/B'.

Fill model (strategy-owned resting book, not a CLOB):
  - resting BUY @ P fills when best ask ≤ P
  - resting SELL @ P fills when best bid ≥ P
Only then emit an OrderIntent with limit_price=P for immediate Portfolio fill.

If both buy legs fill before the exit sell, cancel the exit and hold both to settlement.
"""

from __future__ import annotations

from dataclasses import dataclass

from strategies.base import Action, MarketEndContext, OrderIntent, Side, TickContext
from strategies.fees import CRYPTO_TAKER_FEE_RATE

REASON_PREFIX = "dual_limit|"


@dataclass
class _MarketState:
    posts_armed: bool = False
    rest_buy_up: bool = False
    rest_buy_down: bool = False
    rest_sell_up: bool = False
    rest_sell_down: bool = False
    filled_up: bool = False
    filled_down: bool = False
    hold_both: bool = False
    done: bool = False
    up_shares: float = 0.0
    down_shares: float = 0.0


class DualLimitExitStrategy:
    """Rest fixed A/B buys; one-sided A'/B' sells; both-fill holds to settlement."""

    name = "dual_limit_exit"

    def __init__(
        self,
        buy_up: float = 0.45,
        buy_down: float = 0.45,
        sell_up: float = 0.55,
        sell_down: float = 0.55,
        shares: float = 10.0,
        taker_fee_rate: float = CRYPTO_TAKER_FEE_RATE,
        fee_model: str = "polymarket",
        once_per_market: bool = True,
        min_elapsed_seconds: float = 5.0,
        min_remaining_seconds: float = 10.0,
    ) -> None:
        self.buy_up = float(buy_up)
        self.buy_down = float(buy_down)
        self.sell_up = float(sell_up)
        self.sell_down = float(sell_down)
        self.shares = max(0.0, float(shares))
        self.taker_fee_rate = float(taker_fee_rate)
        self.fee_model = str(fee_model or "polymarket").lower()
        self.once_per_market = bool(once_per_market)
        self.min_elapsed_seconds = max(0.0, float(min_elapsed_seconds))
        self.min_remaining_seconds = max(0.0, float(min_remaining_seconds))
        self._by_market: dict[str, _MarketState] = {}

    def reset(self) -> None:
        self._by_market.clear()

    def _state(self, market_id: str) -> _MarketState:
        st = self._by_market.get(market_id)
        if st is None:
            st = _MarketState()
            self._by_market[market_id] = st
        return st

    def _buy_intent(self, side: Side, price: float, tag: str) -> OrderIntent:
        return OrderIntent(
            side=side,
            action=Action.BUY,
            shares=self.shares,
            limit_price=price,
            fee_rate=self.taker_fee_rate,
            fee_model=self.fee_model,
            reason=f"{REASON_PREFIX}{tag}",
        )

    def _sell_intent(self, side: Side, price: float, shares: float, tag: str) -> OrderIntent:
        return OrderIntent(
            side=side,
            action=Action.SELL,
            shares=shares,
            limit_price=price,
            fee_rate=0.0,
            fee_model="none",
            reason=f"{REASON_PREFIX}{tag}",
        )

    def on_tick(self, ctx: TickContext) -> list[OrderIntent]:
        if self.shares <= 0:
            return []

        st = self._state(ctx.market_id)
        if st.done or st.hold_both:
            return []

        if not st.posts_armed:
            if ctx.elapsed_seconds < self.min_elapsed_seconds:
                return []
            if ctx.remaining_seconds < self.min_remaining_seconds:
                return []
            st.posts_armed = True
            st.rest_buy_up = True
            st.rest_buy_down = True

        intents: list[OrderIntent] = []

        can_buy_up = (
            st.rest_buy_up
            and not st.filled_up
            and ctx.up_ask_price is not None
            and float(ctx.up_ask_price) <= self.buy_up + 1e-12
        )
        can_buy_down = (
            st.rest_buy_down
            and not st.filled_down
            and ctx.down_ask_price is not None
            and float(ctx.down_ask_price) <= self.buy_down + 1e-12
        )

        if can_buy_up and can_buy_down:
            intents.append(self._buy_intent(Side.UP, self.buy_up, "buy_up"))
            intents.append(self._buy_intent(Side.DOWN, self.buy_down, "buy_down"))
            st.rest_buy_up = False
            st.rest_buy_down = False
            st.rest_sell_up = False
            st.rest_sell_down = False
            st.filled_up = True
            st.filled_down = True
            st.up_shares = self.shares
            st.down_shares = self.shares
            st.hold_both = True
            if self.once_per_market:
                st.done = True
            return intents

        if can_buy_up:
            intents.append(self._buy_intent(Side.UP, self.buy_up, "buy_up"))
            st.rest_buy_up = False
            st.rest_buy_down = False
            st.filled_up = True
            st.up_shares = self.shares
            st.rest_sell_up = True

        if can_buy_down:
            intents.append(self._buy_intent(Side.DOWN, self.buy_down, "buy_down"))
            st.rest_buy_up = False
            st.rest_buy_down = False
            st.filled_down = True
            st.down_shares = self.shares
            st.rest_sell_down = True

        # One-sided exit sells (only while the other buy never filled)
        if st.rest_sell_up and st.filled_up and not st.filled_down:
            bid = ctx.up_bid_price
            if bid is not None and float(bid) >= self.sell_up - 1e-12:
                intents.append(
                    self._sell_intent(Side.UP, self.sell_up, st.up_shares, "sell_up")
                )
                st.rest_sell_up = False
                st.up_shares = 0.0
                if self.once_per_market:
                    st.done = True

        if st.rest_sell_down and st.filled_down and not st.filled_up:
            bid = ctx.down_bid_price
            if bid is not None and float(bid) >= self.sell_down - 1e-12:
                intents.append(
                    self._sell_intent(Side.DOWN, self.sell_down, st.down_shares, "sell_down")
                )
                st.rest_sell_down = False
                st.down_shares = 0.0
                if self.once_per_market:
                    st.done = True

        return intents

    def on_market_end(self, ctx: MarketEndContext) -> None:
        self._by_market.pop(ctx.market_id, None)
