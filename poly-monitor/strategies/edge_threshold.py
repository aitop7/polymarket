"""Edge threshold strategy using an external model_p_up on the tick context."""

from __future__ import annotations

from strategies.base import Action, OrderIntent, Side, TickContext


class EdgeThresholdStrategy:
    """If model_p_up - up_price > threshold → BUY UP; symmetric for DOWN."""

    name = "edge_threshold"

    def __init__(
        self,
        threshold: float = 0.05,
        size_usd: float = 10.0,
        once_per_market: bool = True,
    ) -> None:
        self.threshold = float(threshold)
        self.size_usd = float(size_usd)
        self.once_per_market = once_per_market
        self._acted = False

    def reset(self) -> None:
        self._acted = False

    def on_tick(self, ctx: TickContext) -> list[OrderIntent]:
        if self.once_per_market and self._acted:
            return []
        p = ctx.model_p_up
        if p is None:
            return []
        up_edge = p - ctx.up_price
        down_edge = (1.0 - p) - ctx.down_price
        intents: list[OrderIntent] = []
        if up_edge > self.threshold and down_edge <= up_edge:
            intents.append(
                OrderIntent(
                    side=Side.UP,
                    action=Action.BUY,
                    size_usd=self.size_usd,
                    reason=f"up_edge={up_edge:.4f}",
                )
            )
            self._acted = True
        elif down_edge > self.threshold:
            intents.append(
                OrderIntent(
                    side=Side.DOWN,
                    action=Action.BUY,
                    size_usd=self.size_usd,
                    reason=f"down_edge={down_edge:.4f}",
                )
            )
            self._acted = True
        return intents

    def on_market_end(self, ctx) -> None:  # noqa: ANN001
        self.reset()
