"""Edge threshold strategy using an external model_p_up on the tick context."""

from __future__ import annotations

from strategies.base import Action, OrderIntent, Side, TickContext
from strategies.fees import CRYPTO_TAKER_FEE_RATE, buy_cash_required


class EdgeThresholdStrategy:
    """If model_p_up - up_price > threshold → BUY UP; symmetric for DOWN."""

    name = "edge_threshold"

    def __init__(
        self,
        threshold: float = 0.05,
        size_usd: float = 10.0,
        once_per_market: bool = True,
        max_trades_per_market: int | None = None,
        cooldown_seconds: float = 10.0,
        min_elapsed_seconds: float = 5.0,
        min_remaining_seconds: float = 10.0,
    ) -> None:
        self.threshold = float(threshold)
        self.size_usd = float(size_usd)
        self.once_per_market = once_per_market
        if once_per_market:
            self.max_trades_per_market = 1
        else:
            self.max_trades_per_market = max(1, int(max_trades_per_market or 3))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.min_elapsed_seconds = max(0.0, float(min_elapsed_seconds))
        self.min_remaining_seconds = max(0.0, float(min_remaining_seconds))
        self._trades = 0
        self._last_trade_ts: int | None = None

    def reset(self) -> None:
        self._trades = 0
        self._last_trade_ts = None

    def on_tick(self, ctx: TickContext) -> list[OrderIntent]:
        if self._trades >= self.max_trades_per_market:
            return []
        if ctx.elapsed_seconds < self.min_elapsed_seconds:
            return []
        if ctx.remaining_seconds < self.min_remaining_seconds:
            return []
        if self._last_trade_ts is not None and self.cooldown_seconds > 0:
            elapsed_s = (ctx.timestamp - self._last_trade_ts) / 1000.0
            if elapsed_s < self.cooldown_seconds:
                return []

        p = ctx.model_p_up
        if p is None:
            return []

        port = ctx.portfolio
        if port is None or port.cash <= 0:
            return []

        # Need enough cash for at least a minimal clip (price + crypto taker fee).
        est_cost = buy_cash_required(
            1.0,
            max(ctx.up_price, ctx.down_price, 0.05),
            fee_rate=CRYPTO_TAKER_FEE_RATE,
            fee_model="polymarket",
        )
        if port.cash + 1e-9 < min(self.size_usd, est_cost):
            return []

        spend = min(self.size_usd, float(port.cash))
        up_edge = p - ctx.up_price
        down_edge = (1.0 - p) - ctx.down_price
        intents: list[OrderIntent] = []
        if up_edge > self.threshold and down_edge <= up_edge:
            intents.append(
                OrderIntent(
                    side=Side.UP,
                    action=Action.BUY,
                    size_usd=spend,
                    fee_rate=CRYPTO_TAKER_FEE_RATE,
                    fee_model="polymarket",
                    reason=f"up_edge={up_edge:.4f}",
                )
            )
        elif down_edge > self.threshold:
            intents.append(
                OrderIntent(
                    side=Side.DOWN,
                    action=Action.BUY,
                    size_usd=spend,
                    fee_rate=CRYPTO_TAKER_FEE_RATE,
                    fee_model="polymarket",
                    reason=f"down_edge={down_edge:.4f}",
                )
            )
        if intents:
            self._trades += 1
            self._last_trade_ts = ctx.timestamp
        return intents

    def on_market_end(self, ctx) -> None:  # noqa: ANN001
        self.reset()
