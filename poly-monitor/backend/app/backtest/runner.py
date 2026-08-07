"""Multi-market backtest runner."""

from __future__ import annotations

from typing import Any

from app.core.data import list_markets
from app.engine.replay import run_market_backtest


def run_backtest(
    *,
    strategy: str = "lgbm_edge",
    split: str = "validation",
    market_ids: list[str] | None = None,
    limit: int = 20,
    strategy_params: dict[str, Any] | None = None,
    starting_cash: float = 1000.0,
) -> dict[str, Any]:
    if market_ids:
        ids = [str(m) for m in market_ids]
    else:
        markets = list_markets(split, limit=limit)
        ids = [m["market_id"] for m in markets]

    results: list[dict[str, Any]] = []
    total_pnl = 0.0
    total_fills = 0
    wins = 0
    for mid in ids:
        r = run_market_backtest(
            mid,
            split=split,
            strategy_name=strategy,
            strategy_params=strategy_params,
            starting_cash=starting_cash,
        )
        results.append(
            {
                "market_id": r["market_id"],
                "winner": r["winner"],
                "pnl": r["pnl"],
                "n_fills": r["n_fills"],
                "ending_cash": r["ending_cash"],
            }
        )
        total_pnl += float(r["pnl"])
        total_fills += int(r["n_fills"])
        if r["pnl"] > 0:
            wins += 1

    n = len(results) or 1
    equity_curve = []
    cum = 0.0
    for r in results:
        cum += float(r["pnl"])
        equity_curve.append({"market_id": r["market_id"], "cum_pnl": cum})

    return {
        "strategy": strategy,
        "split": split,
        "n_markets": len(results),
        "total_pnl": total_pnl,
        "avg_pnl": total_pnl / n,
        "win_rate": wins / n,
        "total_fills": total_fills,
        "starting_cash": starting_cash,
        "markets": results,
        "equity_curve": equity_curve,
        "params": strategy_params or {},
    }
