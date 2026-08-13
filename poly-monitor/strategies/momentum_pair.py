"""Momentum-pair strategy: predicted UP mid momentum + confirm / fail-switch / hedge.

Algorithm (causal mapping — no true premarket in feature frames):
  P(t)  = model-predicted UP mid at t+T (LightGBM regression)
  P'(t) = (P(t) - P(t-Δ)) / Δ
  U/D   = actual UP/DOWN mid (fallback trade price)
  Entry near open using P,P'; confirm on actual; fail → 2N opposite; else hedge.

Invariant: once any inventory exists, finish the market with equal UP and DOWN shares
(matched pair). The second leg always targets the share deficit, not a fresh USD size.
"""

from __future__ import annotations

from collections import deque
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np

from strategies.base import Action, OrderIntent, Side, TickContext
from strategies.fees import CRYPTO_TAKER_FEE_RATE, buy_cash_required
from strategies.lgbm_edge import FEATURE_COLUMNS, _feature_coverage, _has_any_model_features

_MIN_FEATURE_COVERAGE = 0.35
_HALF = 0.50
_EQ_EPS = 0.05


class _Phase(str, Enum):
    IDLE = "idle"
    WAIT_CONFIRM_UP = "wait_confirm_up"
    WAIT_CONFIRM_DOWN = "wait_confirm_down"
    WAIT_PPRIME_POS = "wait_pprime_pos"  # STEP5
    WAIT_PPRIME_NEG = "wait_pprime_neg"  # STEP7
    HEDGE = "hedge"  # STEP8 — force equal shares
    DONE = "done"


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
        if np.isnan(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


class MomentumPairStrategy:
    """Choose predicted-momentum side → N → confirm → fail with 2N → equal shares."""

    name = "momentum_pair"

    def __init__(
        self,
        model_path: str | Path | None = None,
        size_usd: float = 10.0,
        shares_n: float | None = None,
        horizon_seconds: float = 5.0,
        delta_seconds: float = 1.0,
        min_fail_drop: float = 0.02,
        min_pair_edge: float = 0.0,
        min_elapsed_seconds: float = 0.0,
        min_remaining_seconds: float = 5.0,
        entry_window_seconds: float | None = None,
        cooldown_seconds: float = 0.0,
        fee_rate: float = CRYPTO_TAKER_FEE_RATE,
        fee_model: str = "polymarket",
        min_feature_coverage: float = _MIN_FEATURE_COVERAGE,
        allow_missing_model: bool = False,
    ) -> None:
        self.size_usd = float(size_usd)
        self.shares_n = float(shares_n) if shares_n is not None else None
        self.horizon_seconds = max(0.5, float(horizon_seconds))
        self.delta_seconds = max(0.2, float(delta_seconds))
        self.min_fail_drop = max(0.0, float(min_fail_drop))
        self.min_pair_edge = float(min_pair_edge)
        self.min_elapsed_seconds = max(0.0, float(min_elapsed_seconds))
        self.min_remaining_seconds = max(0.0, float(min_remaining_seconds))
        self.entry_window_seconds = float(
            entry_window_seconds
            if entry_window_seconds is not None
            else self.horizon_seconds
        )
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.fee_rate = float(fee_rate)
        self.fee_model = str(fee_model or "polymarket").lower()
        self.min_feature_coverage = float(min_feature_coverage)

        self.model = None
        if model_path:
            path = Path(model_path)
            if path.is_file():
                import lightgbm as lgb

                self.model = lgb.Booster(model_file=str(path))
            elif not allow_missing_model:
                raise FileNotFoundError(f"momentum_pair model not found: {path}")

        self._phase = _Phase.IDLE
        self._hist: deque[tuple[int, float, float, float]] = deque(maxlen=512)
        # (ts_ms, P, U, D)
        self._last_trade_ts: int | None = None
        self._entry_done = False
        self._entry_n: float | None = None  # locked share size from first leg

    def reset(self) -> None:
        self._phase = _Phase.IDLE
        self._hist.clear()
        self._last_trade_ts = None
        self._entry_done = False
        self._entry_n = None

    def predict_up_mid(self, features: dict[str, Any]) -> float | None:
        if self.model is None:
            return None
        if _has_any_model_features(features):
            if _feature_coverage(features) < self.min_feature_coverage:
                return None
        row: list[float] = []
        for col in FEATURE_COLUMNS:
            v = features.get(col)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                row.append(np.nan)
            else:
                row.append(float(v))
        X = np.asarray([row], dtype=np.float32)
        try:
            p = float(self.model.predict(X)[0])
        except Exception:
            return None
        if not (0.0 <= p <= 1.0):
            if -0.05 <= p <= 1.05:
                return float(min(1.0, max(0.0, p)))
            return None
        return p

    def _actual_up(self, ctx: TickContext) -> float | None:
        feats = ctx.features or {}
        return _f(feats.get("up_mid")) or _f(ctx.up_price) or _f(ctx.up_ask_price)

    def _actual_down(self, ctx: TickContext) -> float | None:
        feats = ctx.features or {}
        return _f(feats.get("down_mid")) or _f(ctx.down_price) or _f(ctx.down_ask_price)

    def _velocity(
        self, series_idx: int, now_ts: int, now_val: float
    ) -> float | None:
        """series_idx: 1=P, 2=U, 3=D in _hist tuples."""
        target = now_ts - int(self.delta_seconds * 1000)
        best: tuple[int, float] | None = None
        for row in self._hist:
            ts = row[0]
            val = row[series_idx]
            if ts > target:
                continue
            if best is None or ts > best[0]:
                best = (ts, val)
        if best is None:
            return None
        dt = (now_ts - best[0]) / 1000.0
        if dt <= 0:
            return None
        return (now_val - best[1]) / dt

    def _ask(self, ctx: TickContext, side: Side) -> float | None:
        if side == Side.UP:
            px = ctx.up_ask_price if ctx.up_ask_price is not None else ctx.up_price
        else:
            px = ctx.down_ask_price if ctx.down_ask_price is not None else ctx.down_price
        if px is None:
            return None
        try:
            v = float(px)
        except (TypeError, ValueError):
            return None
        if not (0.0 < v < 1.0):
            return None
        return v

    def _shares(self, port: Any) -> tuple[float, float]:
        if port is None:
            return 0.0, 0.0
        return float(port.up_shares or 0.0), float(port.down_shares or 0.0)

    def _is_equal(self, up_s: float, down_s: float) -> bool:
        return abs(up_s - down_s) < _EQ_EPS and up_s > 0 and down_s > 0

    def _entry_share_size(self, ctx: TickContext, side: Side) -> float:
        if self._entry_n is not None and self._entry_n > 0:
            return float(self._entry_n)
        if self.shares_n is not None and self.shares_n > 0:
            return float(self.shares_n)
        px = self._ask(ctx, side) or 0.5
        px = min(0.99, max(0.01, float(px)))
        return max(0.01, self.size_usd / px)

    def _affordable_shares(self, ctx: TickContext, side: Side, want: float) -> float:
        """Clamp requested shares to cash; never silently change N without caller knowing."""
        if want <= 0:
            return 0.0
        port = ctx.portfolio
        px = self._ask(ctx, side)
        if px is None or port is None:
            return 0.0
        cash = float(port.cash or 0.0)
        if cash <= 0:
            return 0.0
        need = buy_cash_required(want, float(px), fee_rate=self.fee_rate, fee_model=self.fee_model)
        if need <= cash + 1e-9:
            return float(want)
        # Solve shares for available cash (polymarket fee ≈ p + fee*p*(1-p)).
        p = float(px)
        if self.fee_model == "polymarket" and self.fee_rate > 0:
            denom = p * (1.0 + self.fee_rate * (1.0 - p))
        elif self.fee_model == "flat" and self.fee_rate > 0:
            denom = p * (1.0 + self.fee_rate)
        else:
            denom = p
        if denom <= 0:
            return 0.0
        return max(0.0, cash / denom)

    def _buy(
        self, ctx: TickContext, side: Side, shares: float, reason: str
    ) -> list[OrderIntent]:
        shares = float(shares)
        if shares <= _EQ_EPS:
            return []
        port = ctx.portfolio
        if port is None or float(port.cash or 0.0) <= 0:
            return []
        if self._last_trade_ts is not None and self.cooldown_seconds > 0:
            if (ctx.timestamp - self._last_trade_ts) / 1000.0 < self.cooldown_seconds:
                return []
        px = self._ask(ctx, side)
        if px is None:
            return []
        shares = self._affordable_shares(ctx, side, shares)
        if shares <= _EQ_EPS:
            return []
        self._last_trade_ts = int(ctx.timestamp)
        return [
            OrderIntent(
                side=side,
                action=Action.BUY,
                shares=float(shares),
                size_usd=None,
                limit_price=float(px),
                fee_rate=self.fee_rate,
                fee_model=self.fee_model,
                reason=reason,
            )
        ]

    def _equalize_orders(self, ctx: TickContext, *, reason: str) -> list[OrderIntent]:
        """Buy exactly the share deficit so UP and DOWN match."""
        up_s, down_s = self._shares(ctx.portfolio)
        if self._is_equal(up_s, down_s):
            self._phase = _Phase.DONE
            return []
        if up_s + 1e-9 < down_s:
            need = down_s - up_s
            return self._buy(ctx, Side.UP, need, reason)
        if down_s + 1e-9 < up_s:
            need = up_s - down_s
            return self._buy(ctx, Side.DOWN, need, reason)
        return []

    def _seed_pair_orders(self, ctx: TickContext) -> list[OrderIntent]:
        """Flat book: buy equal UP+DOWN N when ask sum is cheap (< $1)."""
        up_ask = self._ask(ctx, Side.UP)
        down_ask = self._ask(ctx, Side.DOWN)
        if up_ask is None or down_ask is None:
            return []
        if float(up_ask) + float(down_ask) >= 1.0 - self.min_pair_edge:
            return []
        n = self._entry_share_size(ctx, Side.UP)
        n = min(
            n,
            self._affordable_shares(ctx, Side.UP, n),
            self._affordable_shares(ctx, Side.DOWN, n),
        )
        if n <= _EQ_EPS:
            return []
        self._entry_n = float(n)
        self._last_trade_ts = int(ctx.timestamp)
        return [
            OrderIntent(
                side=Side.UP,
                action=Action.BUY,
                shares=float(n),
                limit_price=float(up_ask),
                fee_rate=self.fee_rate,
                fee_model=self.fee_model,
                reason="momentum_pair|STEP8_seed_up",
            ),
            OrderIntent(
                side=Side.DOWN,
                action=Action.BUY,
                shares=float(n),
                limit_price=float(down_ask),
                fee_rate=self.fee_rate,
                fee_model=self.fee_model,
                reason="momentum_pair|STEP8_seed_down",
            ),
        ]

    def on_tick(self, ctx: TickContext) -> list[OrderIntent]:
        if self._phase == _Phase.DONE:
            return []

        # Near expiry: stop waiting on signals — force equal shares.
        if ctx.remaining_seconds < self.min_remaining_seconds:
            up_s, down_s = self._shares(ctx.portfolio)
            if up_s > 0 or down_s > 0:
                self._phase = _Phase.HEDGE
            elif self._phase == _Phase.IDLE:
                return []

        U = self._actual_up(ctx)
        D = self._actual_down(ctx)
        if U is None or D is None:
            return []

        P = self.predict_up_mid(ctx.features or {})
        if P is None:
            P = U

        ts = int(ctx.timestamp)
        P_prime = self._velocity(1, ts, P)
        U_prime = self._velocity(2, ts, U)
        D_prime = self._velocity(3, ts, D)
        self._hist.append((ts, P, U, D))

        ctx.features = dict(ctx.features or {})
        ctx.features["pred_up_mid"] = P
        if P_prime is not None:
            ctx.features["pred_up_vel"] = P_prime

        # --- STEP1 / STEP2 entry once early ---
        if (
            self._phase == _Phase.IDLE
            and not self._entry_done
            and ctx.elapsed_seconds >= self.min_elapsed_seconds
            and ctx.elapsed_seconds <= self.entry_window_seconds
            and P_prime is not None
        ):
            if P > _HALF and P_prime > 0:
                n = self._entry_share_size(ctx, Side.UP)
                orders = self._buy(ctx, Side.UP, n, "momentum_pair|STEP1_buy_up_N")
                if orders:
                    self._entry_n = float(orders[0].shares or n)
                    self._entry_done = True
                    self._phase = _Phase.WAIT_CONFIRM_UP
                    return orders
            if P < _HALF and P_prime < 0:
                n = self._entry_share_size(ctx, Side.DOWN)
                orders = self._buy(ctx, Side.DOWN, n, "momentum_pair|STEP2_buy_down_N")
                if orders:
                    self._entry_n = float(orders[0].shares or n)
                    self._entry_done = True
                    self._phase = _Phase.WAIT_CONFIRM_DOWN
                    return orders
            if ctx.elapsed_seconds > self.entry_window_seconds:
                self._entry_done = True
                self._phase = _Phase.HEDGE

        confirm_deadline = self.entry_window_seconds + self.horizon_seconds

        # --- STEP3 confirm UP ---
        if self._phase == _Phase.WAIT_CONFIRM_UP and U_prime is not None:
            if U > _HALF and U_prime > 0:
                self._phase = _Phase.WAIT_PPRIME_POS
            elif self._fail_up(U, U_prime, P, P_prime):
                # STEP6: buy DOWN 2N (same N as entry), then wait to buy UP N → equal 2N.
                n2 = 2.0 * self._entry_share_size(ctx, Side.DOWN)
                orders = self._buy(ctx, Side.DOWN, n2, "momentum_pair|STEP6_fail_buy_down_2N")
                if orders:
                    self._phase = _Phase.WAIT_PPRIME_NEG
                    return orders
            elif ctx.elapsed_seconds > confirm_deadline:
                self._phase = _Phase.HEDGE

        # --- STEP4 confirm DOWN ---
        if self._phase == _Phase.WAIT_CONFIRM_DOWN and D_prime is not None:
            if D > _HALF and D_prime > 0:
                self._phase = _Phase.WAIT_PPRIME_NEG
            elif ctx.elapsed_seconds > confirm_deadline:
                self._phase = _Phase.HEDGE

        # --- STEP5: after UP confirm, when P' flips ≤0 buy DOWN to match UP shares ---
        if self._phase == _Phase.WAIT_PPRIME_POS and P_prime is not None:
            if P_prime <= 0:
                orders = self._equalize_orders(ctx, reason="momentum_pair|STEP5_buy_down_N")
                if orders:
                    # Assume fill completes the pair; if cash-clipped, HEDGE will finish.
                    up_s, down_s = self._shares(ctx.portfolio)
                    filled = float(orders[0].shares or 0.0)
                    if abs((down_s + filled) - up_s) < _EQ_EPS:
                        self._phase = _Phase.DONE
                    else:
                        self._phase = _Phase.HEDGE
                    return orders
                self._phase = _Phase.HEDGE

        # --- STEP7: after DOWN path, when P' flips ≥0 buy UP to match DOWN shares ---
        if self._phase == _Phase.WAIT_PPRIME_NEG and P_prime is not None:
            if P_prime >= 0:
                orders = self._equalize_orders(ctx, reason="momentum_pair|STEP7_buy_up_N")
                if orders:
                    up_s, down_s = self._shares(ctx.portfolio)
                    filled = float(orders[0].shares or 0.0)
                    if abs((up_s + filled) - down_s) < _EQ_EPS:
                        self._phase = _Phase.DONE
                    else:
                        self._phase = _Phase.HEDGE
                    return orders
                self._phase = _Phase.HEDGE

        # Timed out waiting on P' — still must equalize.
        if self._phase in {_Phase.WAIT_PPRIME_POS, _Phase.WAIT_PPRIME_NEG}:
            if ctx.elapsed_seconds > confirm_deadline + self.horizon_seconds:
                self._phase = _Phase.HEDGE

        # --- STEP8: always finish with equal UP/DOWN shares ---
        if self._phase == _Phase.HEDGE:
            return self._hedge_tick(ctx)

        return []

    def _fail_up(
        self,
        U: float,
        U_prime: float | None,
        P: float,
        P_prime: float | None,
    ) -> bool:
        if U_prime is None or P_prime is None:
            return False
        if not (U_prime < 0 and P_prime < 0):
            return False
        predicted_drop = max(0.0, U - P)
        if predicted_drop < self.min_fail_drop and (_HALF - P) < self.min_fail_drop:
            return False
        return True

    def _hedge_tick(self, ctx: TickContext) -> list[OrderIntent]:
        up_s, down_s = self._shares(ctx.portfolio)

        # Already matched → done (settlement locks $1/share pair PnL).
        if self._is_equal(up_s, down_s):
            self._phase = _Phase.DONE
            return []

        # One-sided (or unequal) inventory: always buy the deficit — do NOT gate on
        # ask-sum < $1. That gate is only for opening a fresh pair from flat.
        if up_s > _EQ_EPS or down_s > _EQ_EPS:
            orders = self._equalize_orders(ctx, reason="momentum_pair|STEP8_equalize")
            if orders:
                return orders
            return []

        # Flat: optional cheap pair seed (equal N both sides, atomic).
        return self._seed_pair_orders(ctx)

    def on_market_end(self, ctx) -> None:  # noqa: ANN001
        self.reset()
