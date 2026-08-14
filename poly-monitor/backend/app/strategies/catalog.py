"""Rich strategy catalog for the Strategy UI (ideas, data, params)."""

from __future__ import annotations

from typing import Any

from app.strategies.registry import list_strategies

_DEFAULT_MIN_ELAPSED_SECONDS = 5.0
_DEFAULT_MIN_REMAINING_SECONDS = 10.0

# Keep descriptions aligned with strategies/*.py
STRATEGY_DOCS: dict[str, dict[str, Any]] = {
    "lgbm_edge": {
        "title": "LightGBM edge",
        "idea": (
            "Predict P(UP) with a LightGBM classifier trained on per-tick market features, "
            "then buy UP or DOWN when model probability minus market mid exceeds a threshold "
            "(same edge rule as edge_threshold)."
        ),
        "when_to_use": "Directional edge vs Polymarket mid when you have labeled feature history.",
        "data_required": [
            {
                "name": "Feature parquets",
                "path": "fetch_real/features/{train,validation,test}/*.parquet",
                "why": "Tick-level feature rows + winner label for supervised training.",
            },
            {
                "name": "Feature schema",
                "path": "fetch_real feature_schema.FEATURE_COLUMNS",
                "why": "Must match columns used at inference (lgbm_edge.FEATURE_COLUMNS).",
            },
            {
                "name": "Trained model file",
                "path": "fetch_real/models/lgbm_baseline.txt",
                "why": "Loaded at runtime by LgbmEdgeStrategy.",
            },
        ],
        "trainable": True,
        "train_defaults": {
            "num_boost_round": 500,
            "early_stopping_rounds": 50,
            "max_markets": None,
            "learning_rate": 0.05,
            "num_leaves": 31,
            "min_child_samples": 50,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "seed": 42,
        },
        "runtime_params": {
            "threshold": 0.05,
            "size_usd": 10.0,
            "once_per_market": True,
            "max_trades_per_market": 3,
            "cooldown_seconds": 10.0,
            "min_elapsed_seconds": _DEFAULT_MIN_ELAPSED_SECONDS,
            "min_remaining_seconds": _DEFAULT_MIN_REMAINING_SECONDS,
            "model_path": "fetch_real/models/lgbm_baseline.txt",
            "min_feature_coverage": 0.35,
        },
        "outputs": [
            "fetch_real/models/lgbm_baseline.txt",
            "fetch_real/models/feature_names.json",
            "fetch_real/models/metrics.json",
        ],
    },
    "edge_threshold": {
        "title": "Edge threshold",
        "idea": (
            "Given an external model_p_up on each tick, buy UP when "
            "model_p_up − up_price > threshold, or buy DOWN when "
            "down_price − (1 − model_p_up) would be the symmetric edge. "
            "Does not train a model — only applies the trading rule."
        ),
        "when_to_use": "You already have P(UP) from another model or feed.",
        "data_required": [
            {
                "name": "Tick context with model_p_up",
                "path": "TickContext.model_p_up",
                "why": "Probability must be supplied each tick; no onboard trainer.",
            },
            {
                "name": "Market mids / asks",
                "path": "up_price / down_price (and book when available)",
                "why": "Edge is measured against the live market price.",
            },
        ],
        "trainable": False,
        "train_defaults": {},
        "runtime_params": {
            "threshold": 0.05,
            "size_usd": 10.0,
            "once_per_market": True,
            "max_trades_per_market": 3,
            "cooldown_seconds": 10.0,
            "min_elapsed_seconds": _DEFAULT_MIN_ELAPSED_SECONDS,
            "min_remaining_seconds": _DEFAULT_MIN_REMAINING_SECONDS,
        },
        "outputs": [],
    },
    "safe_pair": {
        "title": "Safe pair (ask-sum)",
        "idea": (
            "Market-neutral: buy equal shares of UP and DOWN when up_ask + down_ask < $1 "
            "after fees/slippage (net edge ≥ min_edge). Sized by near-ask depth and cash. "
            "No directional ML model."
        ),
        "when_to_use": "Capture underpriced pair inventory when both asks sum below fair value.",
        "data_required": [
            {
                "name": "Top / near ask prices",
                "path": "TickContext.up_ask_price, down_ask_price",
                "why": "Gross pair cost = sum of asks.",
            },
            {
                "name": "Ask depth",
                "path": "up/down ask shares + near-ask depth",
                "why": "Caps fillable pair size.",
            },
            {
                "name": "Portfolio cash",
                "path": "TickContext.portfolio",
                "why": "Pair buys require cash for both legs.",
            },
        ],
        "trainable": False,
        "train_defaults": {},
        "runtime_params": {
            "min_edge": 0.005,
            "size_usd": 25.0,
            "min_ask_shares": 1.0,
            "taker_fee_rate": 0.07,
            "fee_model": "polymarket",
            "slippage": 0.0,
            "once_per_market": False,
            "max_pairs_per_market": 5,
            "cooldown_seconds": 10.0,
            "min_elapsed_seconds": _DEFAULT_MIN_ELAPSED_SECONDS,
            "min_remaining_seconds": _DEFAULT_MIN_REMAINING_SECONDS,
        },
        "outputs": [],
    },
    "momentum_pair": {
        "title": "Momentum pair",
        "idea": (
            "Train a LightGBM regressor for UP mid T seconds ahead (P). "
            "Smooth P/U/D with EMA(ema_period), then take Δ-second momentum on those EMAs. "
            "Early in the window buy N on the side with predicted EMA momentum vs 50¢; "
            "confirm on actual U/D EMA velocity; on fail, martingale-buy the opposite side "
            "to 2× the larger inventory (up to max_doubles times); equalize/fail buys wait "
            "for that side's EMA local minimum before filling; finish with equal UP/DOWN shares."
        ),
        "when_to_use": "Directional open momentum with an explicit fail-switch into a pair.",
        "data_required": [
            {
                "name": "Live VWAP markets",
                "path": "E:\\DataSets\\poly\\live\\YYYY-MM-DD\\<market_id>\\",
                "why": (
                    "fetch_live dirs (pm_orderbooks / pm_chainlink preferred when present; "
                    "else orderbooks / chainlink; plus binance/trades). "
                    "Features engineered on load; markets re-split 80/20 chronologically."
                ),
            },
            {
                "name": "UP mid series",
                "path": "up_mid from book (fallback up_price)",
                "why": (
                    "Regression target y = up_mid(t+T). Runtime U'/D'/P' from EMA(mid) "
                    "with train/runtime ema_period."
                ),
            },
            {
                "name": "Trained price model",
                "path": "data/strategy_versions/momentum_pair/*.model.txt",
                "why": "Loaded at runtime as P(t).",
            },
        ],
        "trainable": True,
        "train_defaults": {
            "horizon_seconds": 5.0,
            "delta_seconds": 1.0,
            "ema_period": 8.0,
            "num_boost_round": 400,
            "early_stopping_rounds": 40,
            "max_markets": None,
            "learning_rate": 0.05,
            "num_leaves": 31,
            "train_ratio": 0.8,
        },
        "runtime_params": {
            "size_usd": 10.0,
            "horizon_seconds": 5.0,
            "delta_seconds": 1.0,
            "ema_period": 8.0,
            "max_doubles": 1,
            "min_fail_drop": 0.02,
            "min_pair_edge": 0.0,
            "min_elapsed_seconds": 0.0,
            "min_remaining_seconds": 5.0,
            "cooldown_seconds": 0.0,
            "fee_model": "polymarket",
            "model_path": "fetch_real/models/momentum_pair_up_mid.txt",
        },
        "outputs": [
            "data/strategy_versions/momentum_pair/<timestamp>.model.txt",
            "data/strategy_versions/momentum_pair/<timestamp>.metrics.json",
            "fetch_real/models/momentum_pair_up_mid.txt",
        ],
    },
}


def catalog_strategies() -> list[dict[str, Any]]:
    """Merge registry list with documentation blocks (skips 'none')."""
    out: list[dict[str, Any]] = []
    for row in list_strategies():
        name = str(row.get("name") or "")
        if name in {"none", ""}:
            continue
        docs = STRATEGY_DOCS.get(name, {})
        out.append(
            {
                **row,
                "title": docs.get("title") or name,
                "idea": docs.get("idea") or row.get("description") or "",
                "when_to_use": docs.get("when_to_use") or "",
                "data_required": docs.get("data_required") or [],
                "trainable": bool(docs.get("trainable")),
                "train_defaults": docs.get("train_defaults") or {},
                "runtime_params": docs.get("runtime_params") or row.get("params") or {},
                "outputs": docs.get("outputs") or [],
            }
        )
    return out


def catalog_strategy(name: str) -> dict[str, Any] | None:
    key = (name or "").strip().lower()
    for row in catalog_strategies():
        if row["name"] == key:
            return row
    return None
