"""User strategy plugins for poly-monitor."""

from strategies.edge_threshold import EdgeThresholdStrategy
from strategies.safe_pair import SafePairStrategy

__all__ = ["EdgeThresholdStrategy", "LgbmEdgeStrategy", "SafePairStrategy"]


def __getattr__(name: str):
    if name == "LgbmEdgeStrategy":
        from strategies.lgbm_edge import LgbmEdgeStrategy

        return LgbmEdgeStrategy
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
