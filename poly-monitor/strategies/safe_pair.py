"""Market-neutral UP+DOWN ask-sum strategy (safe.md core idea).

Buys equal shares of UP and DOWN when up_ask + down_ask < 1 after costs,
sized by near-ask book depth and available cash.

Limitations:
- Taker execution only (buy both asks at top/near-top prices)
- Near depth uses 0–3¢ ask buckets; does not walk deeper price levels
- Simultaneous pair fill or skip — no delayed hedge of one-sided fills
- Fees: Polymarket formula C×feeRate×p×(1−p) when fee_model=polymarket (crypto 0.07)
"""

from __future__ import annotations

from strategies.base import Action, OrderIntent, Side, TickContext
from strategies.fees import CRYPTO_TAKER_FEE_RATE, max_shares_for_cash, pair_cash_per_share, per_share_fee_drag

PAIR_REASON_PREFIX = "safe_pair|"


def _effective_ask_size(top: float | None, near: float | None) -> float | None:
    """Use the larger of top-of-book shares and near-ask bucket depth."""
    if top is None and near is None:
        return None
    t = max(0.0, float(top or 0.0))
    n = max(0.0, float(near or 0.0))
    best = max(t, n)
    return best if best > 0 else None


class SafePairStrategy:
    """Enter when net ask-sum edge clears min_edge; emit matched UP+DOWN buys."""

    name = "safe_pair"

    def __init__(
        self,
        min_edge: float = 0.005,
        size_usd: float = 25.0,
        min_ask_shares: float = 1.0,
        taker_fee_rate: float = CRYPTO_TAKER_FEE_RATE,
        fee_model: str = "polymarket",
        slippage: float = 0.0,
        once_per_market: bool = False,
        max_pairs_per_market: int = 5,
        cooldown_seconds: float = 10.0,
        min_elapsed_seconds: float = 5.0,
        min_remaining_seconds: float = 10.0,
    ) -> None:
        self.min_edge = float(min_edge)
        self.size_usd = float(size_usd)
        self.min_ask_shares = float(min_ask_shares)
        self.taker_fee_rate = float(taker_fee_rate)
        self.fee_model = str(fee_model or "polymarket").lower()
        self.slippage = float(slippage)
        if once_per_market:
            self.max_pairs_per_market = 1
        else:
            self.max_pairs_per_market = max(1, int(max_pairs_per_market))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.min_elapsed_seconds = max(0.0, float(min_elapsed_seconds))
        self.min_remaining_seconds = max(0.0, float(min_remaining_seconds))
        self._pairs_this_market = 0
        self._last_trade_ts: int | None = None

    def reset(self) -> None:
        self._pairs_this_market = 0
        self._last_trade_ts = None

    def opportunity_at_tick(self, ctx: TickContext) -> tuple[bool, float, float]:
        """Static signal check (ignores cooldown, pair limits, cash). Returns (ok, gross, net)."""
        if ctx.elapsed_seconds < self.min_elapsed_seconds:
            return False, 0.0, 0.0
        if ctx.remaining_seconds < self.min_remaining_seconds:
            return False, 0.0, 0.0

        up_ask = ctx.up_ask_price
        down_ask = ctx.down_ask_price
        if up_ask is None or down_ask is None:
            return False, 0.0, 0.0
        if not (0.0 < up_ask < 1.0 and 0.0 < down_ask < 1.0):
            return False, 0.0, 0.0

        up_sz = _effective_ask_size(ctx.up_ask_shares, ctx.up_ask_near_depth)
        down_sz = _effective_ask_size(ctx.down_ask_shares, ctx.down_ask_near_depth)
        if up_sz is None or down_sz is None:
            return False, 0.0, 0.0
        if up_sz < self.min_ask_shares or down_sz < self.min_ask_shares:
            return False, 0.0, 0.0

        gross_cost = up_ask + down_ask
        if gross_cost <= 0:
            return False, 0.0, 0.0
        gross_edge = 1.0 - gross_cost
        up_px = min(0.999, up_ask + self.slippage)
        down_px = min(0.999, down_ask + self.slippage)
        fee_drag = per_share_fee_drag(up_px, self.taker_fee_rate, self.fee_model) + per_share_fee_drag(
            down_px, self.taker_fee_rate, self.fee_model
        )
        slippage_drag = (up_px - up_ask) + (down_px - down_ask)
        net_edge = gross_edge - slippage_drag - fee_drag
        if net_edge < self.min_edge:
            return False, gross_edge, net_edge
        return True, gross_edge, net_edge

    def on_tick(self, ctx: TickContext) -> list[OrderIntent]:
        if self._pairs_this_market >= self.max_pairs_per_market:
            return []
        if ctx.elapsed_seconds < self.min_elapsed_seconds:
            return []
        if ctx.remaining_seconds < self.min_remaining_seconds:
            return []
        if self._last_trade_ts is not None and self.cooldown_seconds > 0:
            elapsed_s = (ctx.timestamp - self._last_trade_ts) / 1000.0
            if elapsed_s < self.cooldown_seconds:
                return []

        ok, gross_edge, net_edge = self.opportunity_at_tick(ctx)
        if not ok:
            return []

        up_ask = ctx.up_ask_price
        down_ask = ctx.down_ask_price
        assert up_ask is not None and down_ask is not None

        up_sz = _effective_ask_size(ctx.up_ask_shares, ctx.up_ask_near_depth)
        down_sz = _effective_ask_size(ctx.down_ask_shares, ctx.down_ask_near_depth)
        assert up_sz is not None and down_sz is not None

        port = ctx.portfolio
        if port is not None:
            if abs(port.up_shares - port.down_shares) > 1e-6:
                return []
            cash = float(port.cash)
        else:
            cash = float("inf")

        up_px = min(0.999, up_ask + self.slippage)
        down_px = min(0.999, down_ask + self.slippage)
        pair_cost = pair_cash_per_share(
            up_px, down_px, fee_rate=self.taker_fee_rate, fee_model=self.fee_model
        )
        if pair_cost <= 0:
            return []

        shares_by_usd = self.size_usd / pair_cost if self.size_usd > 0 else 0.0
        shares_by_cash = (
            max_shares_for_cash(cash, up_px, down_px, fee_rate=self.taker_fee_rate, fee_model=self.fee_model)
            if cash < float("inf")
            else shares_by_usd
        )
        shares = float(min(up_sz, down_sz, shares_by_usd, shares_by_cash))
        if shares + 1e-9 < self.min_ask_shares:
            return []

        reason = (
            f"{PAIR_REASON_PREFIX}gross={gross_edge:.4f}|net={net_edge:.4f}|"
            f"up={up_ask:.4f}|down={down_ask:.4f}|sz={shares:.2f}|fee={self.fee_model}"
        )
        intents = [
            OrderIntent(
                side=Side.UP,
                action=Action.BUY,
                shares=shares,
                limit_price=up_px,
                fee_rate=self.taker_fee_rate,
                fee_model=self.fee_model,
                reason=reason,
            ),
            OrderIntent(
                side=Side.DOWN,
                action=Action.BUY,
                shares=shares,
                limit_price=down_px,
                fee_rate=self.taker_fee_rate,
                fee_model=self.fee_model,
                reason=reason,
            ),
        ]
        self._pairs_this_market += 1
        self._last_trade_ts = ctx.timestamp
        return intents

    def on_market_end(self, ctx) -> None:  # noqa: ANN001
        self.reset()


def parse_edge_from_reason(reason: str) -> tuple[float | None, float | None]:
    """Parse gross/net edge from safe_pair fill reason string."""
    gross: float | None = None
    net: float | None = None
    for part in str(reason or "").split("|"):
        if part.startswith("gross="):
            try:
                gross = float(part[6:])
            except ValueError:
                pass
        elif part.startswith("net="):
            try:
                net = float(part[4:])
            except ValueError:
                pass
    return gross, net


def is_matched_buy_pair(intents: list[OrderIntent]) -> bool:
    """True when intents are a same-size BUY UP + BUY DOWN pair (safe_pair style)."""
    if len(intents) != 2:
        return False
    a, b = intents
    if a.action != Action.BUY or b.action != Action.BUY:
        return False
    sides = {a.side, b.side}
    if sides != {Side.UP, Side.DOWN}:
        return False
    if a.shares is None or b.shares is None:
        return False
    if abs(float(a.shares) - float(b.shares)) > 1e-9:
        return False
    return True
