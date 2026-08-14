"""Momentum-pair strategy: predicted UP mid momentum + confirm / fail-switch / hedge.

Algorithm (causal mapping — no true premarket in feature frames):
  P(t)  = model-predicted UP mid at t+T (LightGBM regression)
  U/D   = actual UP/DOWN mid (fallback trade price)
  Pe/Ue/De = EMA(P/U/D; ema_period) — smooth before momentum
  P'(t) = (Pe(t) - Pe(t-Δ)) / Δ   (same for U', D')
  Entry near open using Pe,P'; confirm on actual EMA velocity.
  On confirm or fail: buy opposite at 2× (while max_doubles remain) at that side's
  EMA local minimum, then buy the deficit at the other side's EMA trough to equalize.
  Always finish equal UP/DOWN shares (cheap pair when doubles land on troughs).

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
_DEFAULT_EMA_PERIOD = 8.0


class _Phase(str, Enum):
    IDLE = "idle"
    WAIT_CONFIRM_UP = "wait_confirm_up"
    WAIT_CONFIRM_DOWN = "wait_confirm_down"
    WAIT_PPRIME_POS = "wait_pprime_pos"  # want DOWN when P' ≤ 0 (then wait De trough)
    WAIT_PPRIME_NEG = "wait_pprime_neg"  # want UP when P' ≥ 0 (then wait Ue trough)
    WAIT_DOWN_TROUGH = "wait_down_trough"  # armed to buy DOWN at De local min
    WAIT_UP_TROUGH = "wait_up_trough"  # armed to buy UP at Ue local min
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
    """Choose predicted-momentum side → N → confirm → fail-double → equal shares."""

    name = "momentum_pair"

    def __init__(
        self,
        model_path: str | Path | None = None,
        size_usd: float = 10.0,
        shares_n: float | None = None,
        horizon_seconds: float = 5.0,
        delta_seconds: float = 1.0,
        ema_period: float = _DEFAULT_EMA_PERIOD,
        max_doubles: int = 1,
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
        self.ema_period = max(1.0, float(ema_period))
        self._ema_alpha = 2.0 / (self.ema_period + 1.0)
        self.max_doubles = max(0, int(max_doubles))
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
        # Hist stores EMA levels: (ts_ms, P_ema, U_ema, D_ema)
        self._hist: deque[tuple[int, float, float, float]] = deque(maxlen=512)
        self._ema_p: float | None = None
        self._ema_u: float | None = None
        self._ema_d: float | None = None
        self._last_trade_ts: int | None = None
        self._entry_done = False
        self._entry_n: float | None = None  # locked share size from first leg
        self._doubles_used = 0
        self._pending_kind: str | None = None  # "entry" | "equalize" | "double"
        self._prev_u_prime: float | None = None
        self._prev_d_prime: float | None = None

    def reset(self) -> None:
        self._phase = _Phase.IDLE
        self._hist.clear()
        self._ema_p = None
        self._ema_u = None
        self._ema_d = None
        self._last_trade_ts = None
        self._entry_done = False
        self._entry_n = None
        self._doubles_used = 0
        self._pending_kind = None
        self._prev_u_prime = None
        self._prev_d_prime = None

    def _ema_step(self, prev: float | None, x: float) -> float:
        if prev is None:
            return x
        a = self._ema_alpha
        return a * x + (1.0 - a) * prev

    def _can_double(self) -> bool:
        return self._doubles_used < self.max_doubles

    @staticmethod
    def _is_local_min(prev_prime: float | None, prime: float | None) -> bool:
        """EMA trough: was falling, now flat/rising."""
        if prev_prime is None or prime is None:
            return False
        return prev_prime < 0.0 and prime >= 0.0

    def _arm_trough(self, side: Side, kind: str) -> None:
        self._pending_kind = kind
        self._phase = (
            _Phase.WAIT_DOWN_TROUGH if side == Side.DOWN else _Phase.WAIT_UP_TROUGH
        )

    def _execute_pending_trough(
        self, ctx: TickContext, side: Side
    ) -> list[OrderIntent]:
        kind = self._pending_kind or "equalize"
        self._pending_kind = None
        if kind == "double":
            orders = self._double_buy(
                ctx,
                side,
                reason=(
                    "momentum_pair|fail_buy_down"
                    if side == Side.DOWN
                    else "momentum_pair|fail_buy_up"
                ),
            )
            if orders:
                # Overweight on `side` — next buy the deficit at the other EMA trough.
                other = Side.UP if side == Side.DOWN else Side.DOWN
                self._arm_trough(other, "equalize")
                return orders
            self._phase = _Phase.HEDGE
            return []
        # equalize
        reason = (
            "momentum_pair|STEP5_buy_down_trough"
            if side == Side.DOWN
            else "momentum_pair|STEP7_buy_up_trough"
        )
        orders = self._equalize_orders(ctx, reason=reason)
        if orders:
            return self._mark_equalized_or_hedge(ctx, orders, buy_side=side)
        self._phase = _Phase.HEDGE
        return []

    def _arm_opposite_leg(self, side: Side) -> None:
        """Prefer martingale 2× while doubles remain; else equalize 1×."""
        if self._can_double():
            self._arm_trough(side, "double")
        else:
            self._arm_trough(side, "equalize")

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
        """series_idx: 1=P_ema, 2=U_ema, 3=D_ema in _hist tuples."""
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

    def _double_buy(
        self, ctx: TickContext, side: Side, *, reason: str
    ) -> list[OrderIntent]:
        """Martingale: buy ``side`` so its inventory reaches 2× current larger side."""
        if not self._can_double():
            return []
        up_s, down_s = self._shares(ctx.portfolio)
        larger = max(up_s, down_s)
        if larger <= _EQ_EPS:
            larger = self._entry_share_size(ctx, side)
        target = 2.0 * larger
        need = target - (down_s if side == Side.DOWN else up_s)
        if need <= _EQ_EPS:
            return []
        orders = self._buy(
            ctx,
            side,
            need,
            f"{reason}|double_{self._doubles_used + 1}_of_{self.max_doubles}",
        )
        if orders:
            self._doubles_used += 1
            filled = float(orders[0].shares or 0.0)
            # Keep entry_n as the original base; target inventory grows with doubles.
            if self._entry_n is None or self._entry_n <= 0:
                self._entry_n = max(larger, filled / 2.0 if filled > 0 else larger)
        return orders

    def _mark_equalized_or_hedge(
        self, ctx: TickContext, orders: list[OrderIntent], *, buy_side: Side
    ) -> list[OrderIntent]:
        up_s, down_s = self._shares(ctx.portfolio)
        filled = float(orders[0].shares or 0.0) if orders else 0.0
        if buy_side == Side.DOWN:
            ok = abs((down_s + filled) - up_s) < _EQ_EPS
        else:
            ok = abs((up_s + filled) - down_s) < _EQ_EPS
        self._phase = _Phase.DONE if ok else _Phase.HEDGE
        return orders

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
        # Trough-armed legs still fill below (force_flat); don't skip them.
        if ctx.remaining_seconds < self.min_remaining_seconds:
            if self._phase not in {_Phase.WAIT_UP_TROUGH, _Phase.WAIT_DOWN_TROUGH}:
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

        # Smooth levels first; momentum is taken on the EMAs (ema_period=1 → raw).
        self._ema_p = self._ema_step(self._ema_p, P)
        self._ema_u = self._ema_step(self._ema_u, U)
        self._ema_d = self._ema_step(self._ema_d, D)
        Pe = float(self._ema_p)
        Ue = float(self._ema_u)
        De = float(self._ema_d)

        ts = int(ctx.timestamp)
        P_prime = self._velocity(1, ts, Pe)
        U_prime = self._velocity(2, ts, Ue)
        D_prime = self._velocity(3, ts, De)
        self._hist.append((ts, Pe, Ue, De))

        ctx.features = dict(ctx.features or {})
        ctx.features["pred_up_mid"] = P
        ctx.features["pred_up_ema"] = Pe
        ctx.features["up_ema"] = Ue
        ctx.features["down_ema"] = De
        if P_prime is not None:
            ctx.features["pred_up_vel"] = P_prime
        ctx.features["doubles_used"] = float(self._doubles_used)
        ctx.features["max_doubles"] = float(self.max_doubles)

        # --- STEP1 / STEP2 entry once early (immediate; hedge legs wait for trough) ---
        if (
            self._phase == _Phase.IDLE
            and not self._entry_done
            and ctx.elapsed_seconds >= self.min_elapsed_seconds
            and ctx.elapsed_seconds <= self.entry_window_seconds
            and P_prime is not None
        ):
            if Pe > _HALF and P_prime > 0:
                n = self._entry_share_size(ctx, Side.UP)
                orders = self._buy(ctx, Side.UP, n, "momentum_pair|STEP1_buy_up_N")
                if orders:
                    self._entry_n = float(orders[0].shares or n)
                    self._entry_done = True
                    self._phase = _Phase.WAIT_CONFIRM_UP
                    self._prev_u_prime = U_prime
                    self._prev_d_prime = D_prime
                    return orders
            if Pe < _HALF and P_prime < 0:
                n = self._entry_share_size(ctx, Side.DOWN)
                orders = self._buy(ctx, Side.DOWN, n, "momentum_pair|STEP2_buy_down_N")
                if orders:
                    self._entry_n = float(orders[0].shares or n)
                    self._entry_done = True
                    self._phase = _Phase.WAIT_CONFIRM_DOWN
                    self._prev_u_prime = U_prime
                    self._prev_d_prime = D_prime
                    return orders
        if (
            self._phase == _Phase.IDLE
            and not self._entry_done
            and ctx.elapsed_seconds > self.entry_window_seconds
        ):
            self._entry_done = True
            self._phase = _Phase.HEDGE

        confirm_deadline = self.entry_window_seconds + self.horizon_seconds
        # Wait for EMA trough until near expiry — do not abort after a few seconds.
        force_flat = ctx.remaining_seconds < self.min_remaining_seconds

        # --- Fill armed hedge/fail buys at EMA local minimum ---
        if self._phase == _Phase.WAIT_UP_TROUGH:
            if self._is_local_min(self._prev_u_prime, U_prime) or force_flat:
                if force_flat and not self._is_local_min(self._prev_u_prime, U_prime):
                    # Near expiry: finish the pair even if trough never printed.
                    self._pending_kind = "equalize"
                orders = self._execute_pending_trough(ctx, Side.UP)
                self._prev_u_prime = U_prime
                self._prev_d_prime = D_prime
                return orders

        if self._phase == _Phase.WAIT_DOWN_TROUGH:
            if self._is_local_min(self._prev_d_prime, D_prime) or force_flat:
                if force_flat and not self._is_local_min(self._prev_d_prime, D_prime):
                    self._pending_kind = "equalize"
                orders = self._execute_pending_trough(ctx, Side.DOWN)
                self._prev_u_prime = U_prime
                self._prev_d_prime = D_prime
                return orders

        # --- STEP3 confirm UP / fail-double DOWN (at De trough) ---
        if self._phase == _Phase.WAIT_CONFIRM_UP and U_prime is not None:
            if Ue > _HALF and U_prime > 0:
                self._phase = _Phase.WAIT_PPRIME_POS
            elif self._fail_up(Ue, U_prime, Pe, P_prime):
                self._arm_opposite_leg(Side.DOWN)
            elif ctx.elapsed_seconds > confirm_deadline:
                # No confirm in time — still try opposite leg (2× if allowed).
                self._arm_opposite_leg(Side.DOWN)

        # --- STEP4 confirm DOWN / fail-double UP (at Ue trough) ---
        if self._phase == _Phase.WAIT_CONFIRM_DOWN and D_prime is not None:
            if De > _HALF and D_prime > 0:
                self._phase = _Phase.WAIT_PPRIME_NEG
            elif self._fail_down(De, D_prime, Pe, P_prime):
                self._arm_opposite_leg(Side.UP)
            elif ctx.elapsed_seconds > confirm_deadline:
                self._arm_opposite_leg(Side.UP)

        # --- After UP-dominant: when P'≤0 buy DOWN 2× (or equalize) at De trough ---
        if self._phase == _Phase.WAIT_PPRIME_POS and P_prime is not None:
            if P_prime <= 0 or self._fail_up(Ue, U_prime, Pe, P_prime):
                self._arm_opposite_leg(Side.DOWN)

        # --- After DOWN-dominant: when P'≥0 buy UP 2× (or equalize) at Ue trough ---
        if self._phase == _Phase.WAIT_PPRIME_NEG and P_prime is not None:
            if P_prime >= 0 or self._fail_down(De, D_prime, Pe, P_prime):
                self._arm_opposite_leg(Side.UP)

        # Timed out waiting on P' — still take opposite leg / hedge.
        if self._phase in {_Phase.WAIT_PPRIME_POS, _Phase.WAIT_PPRIME_NEG}:
            if ctx.elapsed_seconds > confirm_deadline + self.horizon_seconds or force_flat:
                if self._phase == _Phase.WAIT_PPRIME_POS:
                    self._arm_opposite_leg(Side.DOWN)
                else:
                    self._arm_opposite_leg(Side.UP)

        # --- STEP8: always finish with equal UP/DOWN shares ---
        if self._phase == _Phase.HEDGE:
            self._prev_u_prime = U_prime
            self._prev_d_prime = D_prime
            return self._hedge_tick(ctx)

        self._prev_u_prime = U_prime
        self._prev_d_prime = D_prime
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

    def _fail_down(
        self,
        D: float,
        D_prime: float | None,
        P: float,
        P_prime: float | None,
    ) -> bool:
        if D_prime is None or P_prime is None:
            return False
        if not (D_prime < 0 and P_prime > 0):
            return False
        pred_down = 1.0 - P
        predicted_drop = max(0.0, D - pred_down)
        if predicted_drop < self.min_fail_drop and (P - _HALF) < self.min_fail_drop:
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
