"""Strategy registry / factory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.config import settings

_DEFAULT_MIN_ELAPSED_SECONDS = 5.0
_DEFAULT_MIN_REMAINING_SECONDS = 10.0


def list_strategies() -> list[dict[str, Any]]:
    return [
        {
            "name": "edge_threshold",
            "description": "Trade when external model_p_up edge vs market exceeds threshold",
            "params": {
                "threshold": 0.05,
                "size_usd": 10.0,
                "once_per_market": True,
                "max_trades_per_market": 3,
                "cooldown_seconds": 10.0,
                "min_elapsed_seconds": _DEFAULT_MIN_ELAPSED_SECONDS,
                "min_remaining_seconds": _DEFAULT_MIN_REMAINING_SECONDS,
            },
        },
        {
            "name": "lgbm_edge",
            "description": "LightGBM P(UP) + edge threshold (fetch_real baseline)",
            "params": {
                "threshold": 0.05,
                "size_usd": 10.0,
                "once_per_market": True,
                "max_trades_per_market": 3,
                "cooldown_seconds": 10.0,
                "min_elapsed_seconds": _DEFAULT_MIN_ELAPSED_SECONDS,
                "min_remaining_seconds": _DEFAULT_MIN_REMAINING_SECONDS,
            },
        },
        {
            "name": "safe_pair",
            "description": "Market-neutral: buy equal UP+DOWN when ask sum < $1 after costs",
            "params": {
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
        },
        {
            "name": "none",
            "description": "No automated strategy (manual / monitor only)",
            "params": {},
        },
    ]


def create_strategy(name: str, params: dict[str, Any] | None = None) -> Any:
    params = dict(params or {})
    name = (name or "none").strip().lower()
    if name in {"none", "", "null"}:
        return None
    if name == "edge_threshold":
        from strategies.edge_threshold import EdgeThresholdStrategy

        return EdgeThresholdStrategy(
            threshold=float(params.get("threshold", 0.05)),
            size_usd=float(params.get("size_usd", 10.0)),
            once_per_market=bool(params.get("once_per_market", True)),
            max_trades_per_market=int(params.get("max_trades_per_market", 3)) if params.get("max_trades_per_market") is not None else None,
            cooldown_seconds=float(params.get("cooldown_seconds", 10.0)),
            min_elapsed_seconds=float(params.get("min_elapsed_seconds", _DEFAULT_MIN_ELAPSED_SECONDS)),
            min_remaining_seconds=float(params.get("min_remaining_seconds", _DEFAULT_MIN_REMAINING_SECONDS)),
        )
    if name == "lgbm_edge":
        from strategies.lgbm_edge import LgbmEdgeStrategy

        model_path = Path(params.get("model_path") or (settings.models_dir / "lgbm_baseline.txt"))
        return LgbmEdgeStrategy(
            model_path=model_path,
            threshold=float(params.get("threshold", 0.05)),
            size_usd=float(params.get("size_usd", 10.0)),
            once_per_market=bool(params.get("once_per_market", True)),
            max_trades_per_market=int(params.get("max_trades_per_market", 3)) if params.get("max_trades_per_market") is not None else None,
            cooldown_seconds=float(params.get("cooldown_seconds", 10.0)),
            min_elapsed_seconds=float(params.get("min_elapsed_seconds", _DEFAULT_MIN_ELAPSED_SECONDS)),
            min_remaining_seconds=float(params.get("min_remaining_seconds", _DEFAULT_MIN_REMAINING_SECONDS)),
        )
    if name == "safe_pair":
        from strategies.safe_pair import SafePairStrategy

        return SafePairStrategy(
            min_edge=float(params.get("min_edge", params.get("threshold", 0.005))),
            size_usd=float(params.get("size_usd", 25.0)),
            min_ask_shares=float(params.get("min_ask_shares", 1.0)),
            taker_fee_rate=float(params.get("taker_fee_rate", 0.07)),
            fee_model=str(params.get("fee_model", "polymarket")),
            slippage=float(params.get("slippage", 0.0)),
            once_per_market=bool(params.get("once_per_market", False)),
            max_pairs_per_market=int(params.get("max_pairs_per_market", 5)),
            cooldown_seconds=float(params.get("cooldown_seconds", 10.0)),
            min_elapsed_seconds=float(params.get("min_elapsed_seconds", _DEFAULT_MIN_ELAPSED_SECONDS)),
            min_remaining_seconds=float(params.get("min_remaining_seconds", _DEFAULT_MIN_REMAINING_SECONDS)),
        )
    raise ValueError(f"Unknown strategy: {name}")
