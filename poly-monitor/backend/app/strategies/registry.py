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
            "name": "momentum_pair",
            "description": (
                "Predict UP mid T-ahead; buy N on predicted momentum, confirm, "
                "fail-switch 2N, then hedge to equal shares under $1"
            ),
            "params": {
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
            },
        },
        {
            "name": "dual_limit_exit",
            "description": (
                "Rest BUY UP@A + DOWN@B; on one-sided fill rest SELL at A'/B'; "
                "if both buys fill, hold both to settlement"
            ),
            "params": {
                "buy_up": 0.45,
                "buy_down": 0.45,
                "sell_up": 0.55,
                "sell_down": 0.55,
                "shares": 10.0,
                "taker_fee_rate": 0.07,
                "fee_model": "polymarket",
                "once_per_market": True,
                "min_elapsed_seconds": _DEFAULT_MIN_ELAPSED_SECONDS,
                "min_remaining_seconds": _DEFAULT_MIN_REMAINING_SECONDS,
            },
        },
        {
            "name": "equal_pair_ab",
            "description": (
                "Equal UP+DOWN shares: first leg ask≤B, second leg so sum≤A; "
                "hold completed pairs to settlement (outcome-neutral)"
            ),
            "params": {
                "pair_max": 0.95,
                "first_max": 0.45,
                "shares": 10.0,
                "taker_fee_rate": 0.07,
                "fee_model": "polymarket",
                "once_per_market": True,
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
    if name == "momentum_pair":
        from strategies.momentum_pair import MomentumPairStrategy

        default_model = (
            settings.models_dir / "momentum_pair_up_mid.txt"
            if (settings.models_dir / "momentum_pair_up_mid.txt").is_file()
            else None
        )
        # Prefer active version artifact when present.
        active_rp: dict[str, Any] = {}
        try:
            from app.core.strategy_versions import get_active, strategy_dir

            active = get_active("momentum_pair")
            ver = active.get("version") or {}
            arts = ver.get("artifacts") or {}
            if arts.get("model") and ver.get("id"):
                cand = strategy_dir("momentum_pair") / str(arts["model"])
                if cand.is_file():
                    default_model = cand
            rp = ver.get("runtime_params") or {}
            if isinstance(rp, dict):
                active_rp = dict(rp)
                if rp.get("model_path"):
                    mp = Path(str(rp["model_path"]))
                    if mp.is_file():
                        default_model = mp
        except Exception:
            pass
        # Fill missing runtime knobs from the active version (model_path already handled).
        for k in (
            "horizon_seconds",
            "delta_seconds",
            "ema_period",
            "max_doubles",
            "min_fail_drop",
            "min_pair_edge",
            "size_usd",
        ):
            if params.get(k) is None and active_rp.get(k) is not None:
                params[k] = active_rp[k]
        model_path = params.get("model_path") or default_model
        return MomentumPairStrategy(
            model_path=model_path,
            size_usd=float(params.get("size_usd", 10.0)),
            shares_n=float(params["shares_n"]) if params.get("shares_n") is not None else None,
            horizon_seconds=float(params.get("horizon_seconds", params.get("T", 5.0))),
            delta_seconds=float(params.get("delta_seconds", 1.0)),
            ema_period=float(params.get("ema_period", 8.0)),
            max_doubles=int(params.get("max_doubles", 1)),
            min_fail_drop=float(params.get("min_fail_drop", 0.02)),
            min_pair_edge=float(params.get("min_pair_edge", 0.0)),
            min_elapsed_seconds=float(params.get("min_elapsed_seconds", 0.0)),
            min_remaining_seconds=float(params.get("min_remaining_seconds", 5.0)),
            entry_window_seconds=(
                float(params["entry_window_seconds"])
                if params.get("entry_window_seconds") is not None
                else None
            ),
            cooldown_seconds=float(params.get("cooldown_seconds", 0.0)),
            fee_rate=float(params.get("fee_rate", params.get("taker_fee_rate", 0.07))),
            fee_model=str(params.get("fee_model", "polymarket")),
            allow_missing_model=bool(params.get("allow_missing_model", True)),
        )
    if name == "dual_limit_exit":
        from strategies.dual_limit_exit import DualLimitExitStrategy

        return DualLimitExitStrategy(
            buy_up=float(params.get("buy_up", params.get("A", 0.45))),
            buy_down=float(params.get("buy_down", params.get("B", 0.45))),
            sell_up=float(params.get("sell_up", params.get("A_prime", params.get("A'", 0.55)))),
            sell_down=float(params.get("sell_down", params.get("B_prime", params.get("B'", 0.55)))),
            shares=float(params.get("shares", 10.0)),
            taker_fee_rate=float(params.get("taker_fee_rate", 0.07)),
            fee_model=str(params.get("fee_model", "polymarket")),
            once_per_market=bool(params.get("once_per_market", True)),
            min_elapsed_seconds=float(
                params.get("min_elapsed_seconds", _DEFAULT_MIN_ELAPSED_SECONDS)
            ),
            min_remaining_seconds=float(
                params.get("min_remaining_seconds", _DEFAULT_MIN_REMAINING_SECONDS)
            ),
        )
    if name == "equal_pair_ab":
        from strategies.equal_pair_ab import EqualPairAbStrategy

        return EqualPairAbStrategy(
            pair_max=float(params.get("pair_max", params.get("A", 0.95))),
            first_max=float(params.get("first_max", params.get("B", 0.45))),
            shares=float(params.get("shares", 10.0)),
            taker_fee_rate=float(params.get("taker_fee_rate", 0.07)),
            fee_model=str(params.get("fee_model", "polymarket")),
            once_per_market=bool(params.get("once_per_market", True)),
            min_elapsed_seconds=float(
                params.get("min_elapsed_seconds", _DEFAULT_MIN_ELAPSED_SECONDS)
            ),
            min_remaining_seconds=float(
                params.get("min_remaining_seconds", _DEFAULT_MIN_REMAINING_SECONDS)
            ),
        )
    raise ValueError(f"Unknown strategy: {name}")
