"""Equal-share UP+DOWN pair: first leg ≤ B, pair sum ≤ A, hold to settlement.

State machine (strategy-owned resting limits):
  1. After time gates, rest BUY UP@B and BUY DOWN@B (first-leg candidates).
  2. First fill (ask ≤ B): cancel the other first rest; rest the missing side at
     limit = A − first_fill_price so UP+DOWN ≤ A.
  3. Same tick both asks ≤ B and sum ≤ A: buy both immediately (complete pair).
  4. Once both legs filled: hold equal shares to settlement (outcome-neutral).

Fill model:
  - resting BUY @ P fills when best ask ≤ P
  - emit OrderIntent(limit_price=P) only when crossed
"""

from __future__ import annotations

from dataclasses import dataclass

from strategies.base import Action, MarketEndContext, OrderIntent, Side, TickContext
from strategies.fees import CRYPTO_TAKER_FEE_RATE

REASON_PREFIX = "equal_pair|"


@dataclass
class _MarketState:
    posts_armed: bool = False
    rest_first_up: bool = False
    rest_first_down: bool = False
    rest_second_up: bool = False
    rest_second_down: bool = False
    second_limit: float | None = None
    filled_up: bool = False
    filled_down: bool = False
    first_price: float | None = None
    done: bool = False


class EqualPairAbStrategy:
    """Buy equal UP+DOWN with first leg ≤ B and pair cost ≤ A; hold to settle."""

    name = "equal_pair_ab"

    def __init__(
        self,
        pair_max: float = 0.95,
        first_max: float = 0.45,
        shares: float = 10.0,
        taker_fee_rate: float = CRYPTO_TAKER_FEE_RATE,
        fee_model: str = "polymarket",
        once_per_market: bool = True,
        min_elapsed_seconds: float = 5.0,
        min_remaining_seconds: float = 10.0,
    ) -> None:
        self.pair_max = float(pair_max)  # A
        self.first_max = float(first_max)  # B
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

    def _buy(self, side: Side, price: float, tag: str) -> OrderIntent:
        return OrderIntent(
            side=side,
            action=Action.BUY,
            shares=self.shares,
            limit_price=price,
            fee_rate=self.taker_fee_rate,
            fee_model=self.fee_model,
            reason=f"{REASON_PREFIX}{tag}",
        )

    def _mark_done(self, st: _MarketState) -> None:
        st.rest_first_up = False
        st.rest_first_down = False
        st.rest_second_up = False
        st.rest_second_down = False
        st.second_limit = None
        if self.once_per_market:
            st.done = True

    def on_tick(self, ctx: TickContext) -> list[OrderIntent]:
        if self.shares <= 0 or self.pair_max <= 0 or self.first_max <= 0:
            return []

        st = self._state(ctx.market_id)
        if st.done or (st.filled_up and st.filled_down):
            return []

        if not st.posts_armed:
            if ctx.elapsed_seconds < self.min_elapsed_seconds:
                return []
            if ctx.remaining_seconds < self.min_remaining_seconds:
                return []
            st.posts_armed = True
            st.rest_first_up = True
            st.rest_first_down = True

        intents: list[OrderIntent] = []
        up_ask = ctx.up_ask_price
        down_ask = ctx.down_ask_price

        # --- Second leg (complete the pair) ---
        if st.rest_second_up and not st.filled_up and st.second_limit is not None:
            lim = float(st.second_limit)
            if up_ask is not None and float(up_ask) <= lim + 1e-12:
                px = float(up_ask)
                intents.append(self._buy(Side.UP, px, f"second_up|px={px:.4f}|lim={lim:.4f}"))
                st.filled_up = True
                self._mark_done(st)
                return intents

        if st.rest_second_down and not st.filled_down and st.second_limit is not None:
            lim = float(st.second_limit)
            if down_ask is not None and float(down_ask) <= lim + 1e-12:
                px = float(down_ask)
                intents.append(self._buy(Side.DOWN, px, f"second_down|px={px:.4f}|lim={lim:.4f}"))
                st.filled_down = True
                self._mark_done(st)
                return intents

        # --- First leg(s) ---
        if not st.filled_up and not st.filled_down:
            can_up = (
                st.rest_first_up
                and up_ask is not None
                and float(up_ask) <= self.first_max + 1e-12
            )
            can_down = (
                st.rest_first_down
                and down_ask is not None
                and float(down_ask) <= self.first_max + 1e-12
            )

            # Both first-eligible and pair sum ≤ A → complete pair now
            if can_up and can_down:
                u = float(up_ask)  # type: ignore[arg-type]
                d = float(down_ask)  # type: ignore[arg-type]
                if u + d <= self.pair_max + 1e-12:
                    intents.append(self._buy(Side.UP, u, f"pair_up|sum={u + d:.4f}"))
                    intents.append(self._buy(Side.DOWN, d, f"pair_down|sum={u + d:.4f}"))
                    st.filled_up = True
                    st.filled_down = True
                    st.first_price = min(u, d)
                    self._mark_done(st)
                    return intents
                # Sum too high: take the cheaper as first only
                if u <= d:
                    can_down = False
                else:
                    can_up = False

            if can_up:
                px = float(up_ask)  # type: ignore[arg-type]
                intents.append(self._buy(Side.UP, px, f"first_up|px={px:.4f}"))
                st.filled_up = True
                st.first_price = px
                st.rest_first_up = False
                st.rest_first_down = False
                second = self.pair_max - px
                if second > 1e-9:
                    st.rest_second_down = True
                    st.second_limit = second
                else:
                    self._mark_done(st)
                return intents

            if can_down:
                px = float(down_ask)  # type: ignore[arg-type]
                intents.append(self._buy(Side.DOWN, px, f"first_down|px={px:.4f}"))
                st.filled_down = True
                st.first_price = px
                st.rest_first_up = False
                st.rest_first_down = False
                second = self.pair_max - px
                if second > 1e-9:
                    st.rest_second_up = True
                    st.second_limit = second
                else:
                    self._mark_done(st)
                return intents

        return intents

    def on_market_end(self, ctx: MarketEndContext) -> None:
        self._by_market.pop(ctx.market_id, None)
