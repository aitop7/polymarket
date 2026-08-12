"""LightGBM edge strategy using fetch_real baseline model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np

from strategies.base import TickContext
from strategies.edge_threshold import EdgeThresholdStrategy

# Keep in sync with fetch_real feature_schema.FEATURE_COLUMNS
FEATURE_COLUMNS = [
    "btc_return_1s",
    "btc_return_5s",
    "btc_return_10s",
    "btc_return_30s",
    "btc_return_60s",
    "btc_momentum_10s",
    "btc_momentum_30s",
    "btc_volatility_10s",
    "btc_volatility_30s",
    "btc_volatility_60s",
    "btc_from_open",
    "btc_high_30s_distance",
    "btc_low_30s_distance",
    "up_spread",
    "down_spread",
    "up_mid",
    "down_mid",
    "up_bid_depth",
    "up_ask_depth",
    "down_bid_depth",
    "down_ask_depth",
    "up_order_imbalance",
    "down_order_imbalance",
    "up_near_bid_ratio",
    "up_near_ask_ratio",
    "down_near_bid_ratio",
    "down_near_ask_ratio",
    "trade_count_5s",
    "buy_volume_5s",
    "sell_volume_5s",
    "up_buy_volume_5s",
    "down_buy_volume_5s",
    "trade_count_10s",
    "buy_volume_10s",
    "sell_volume_10s",
    "up_buy_volume_10s",
    "down_buy_volume_10s",
    "trade_count_30s",
    "buy_volume_30s",
    "sell_volume_30s",
    "up_buy_volume_30s",
    "down_buy_volume_30s",
    "trade_imbalance_5s",
    "trade_imbalance_10s",
    "trade_imbalance_30s",
    "market_probability_gap",
    "up_price_change_5s",
    "up_price_change_10s",
    "up_price_change_30s",
    "btc_market_divergence_10s",
    "btc_market_divergence_30s",
    "elapsed_seconds",
    "remaining_seconds",
    "market_progress",
]

_MIN_FEATURE_COVERAGE = 0.35


def _feature_coverage(features: dict[str, Any]) -> float:
    if not features:
        return 0.0
    present = 0
    for col in FEATURE_COLUMNS:
        v = features.get(col)
        if v is None:
            continue
        if isinstance(v, float) and np.isnan(v):
            continue
        present += 1
    return present / len(FEATURE_COLUMNS)


def _has_any_model_features(features: dict[str, Any]) -> bool:
    for col in FEATURE_COLUMNS:
        if col not in features:
            continue
        v = features[col]
        if v is None:
            continue
        if isinstance(v, float) and np.isnan(v):
            continue
        return True
    return False


class LgbmEdgeStrategy:
    name = "lgbm_edge"

    def __init__(
        self,
        model_path: str | Path,
        threshold: float = 0.05,
        size_usd: float = 10.0,
        once_per_market: bool = True,
        max_trades_per_market: int | None = None,
        cooldown_seconds: float = 10.0,
        min_elapsed_seconds: float = 5.0,
        min_remaining_seconds: float = 10.0,
        min_feature_coverage: float = _MIN_FEATURE_COVERAGE,
    ) -> None:
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(f"LightGBM model not found: {path}")
        self.model = lgb.Booster(model_file=str(path))
        self.min_feature_coverage = float(min_feature_coverage)
        self._edge = EdgeThresholdStrategy(
            threshold=threshold,
            size_usd=size_usd,
            once_per_market=once_per_market,
            max_trades_per_market=max_trades_per_market,
            cooldown_seconds=cooldown_seconds,
            min_elapsed_seconds=min_elapsed_seconds,
            min_remaining_seconds=min_remaining_seconds,
        )

    def reset(self) -> None:
        self._edge.reset()

    def predict_p_up(self, features: dict[str, Any]) -> float | None:
        if _has_any_model_features(features):
            if _feature_coverage(features) < self.min_feature_coverage:
                return None
        row = []
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
            return None
        return p

    def on_tick(self, ctx: TickContext) -> list:
        p = self.predict_p_up(ctx.features)
        ctx.model_p_up = p
        return self._edge.on_tick(ctx)

    def on_market_end(self, ctx) -> None:  # noqa: ANN001
        self._edge.on_market_end(ctx)
