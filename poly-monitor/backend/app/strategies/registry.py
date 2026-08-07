"""Strategy registry / factory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.config import settings


def list_strategies() -> list[dict[str, Any]]:
    return [
        {
            "name": "edge_threshold",
            "description": "Trade when external model_p_up edge vs market exceeds threshold",
            "params": {"threshold": 0.05, "size_usd": 10.0, "once_per_market": True},
        },
        {
            "name": "lgbm_edge",
            "description": "LightGBM P(UP) + edge threshold (fetch_real baseline)",
            "params": {"threshold": 0.05, "size_usd": 10.0, "once_per_market": True},
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
        )
    if name == "lgbm_edge":
        from strategies.lgbm_edge import LgbmEdgeStrategy

        model_path = Path(params.get("model_path") or (settings.models_dir / "lgbm_baseline.txt"))
        return LgbmEdgeStrategy(
            model_path=model_path,
            threshold=float(params.get("threshold", 0.05)),
            size_usd=float(params.get("size_usd", 10.0)),
            once_per_market=bool(params.get("once_per_market", True)),
        )
    raise ValueError(f"Unknown strategy: {name}")
