"""Multi-market backtest runner."""

from __future__ import annotations

from typing import Any

from app.core.data import TWAP_SPLIT, list_markets
from app.core.market_index import (
    build_market_index,
    filter_history_markets,
    list_markets_for_date,
)
from app.engine.portfolio import Portfolio
from app.engine.replay import run_market_backtest
from app.strategies.registry import create_strategy


def _resolve_market_ids(
    *,
    split: str,
    market_ids: list[str] | None,
    limit: int,
    date: str | None,
) -> list[str]:
    if market_ids:
        return [str(m) for m in market_ids]

    if date:
        rows = list_markets_for_date(split, date)
        rows.sort(key=lambda r: int(r.get("start_time") or 0))
        if limit:
            rows = rows[: max(0, int(limit))]
        return [str(m["market_id"]) for m in rows]

    if split == TWAP_SPLIT:
        rows = filter_history_markets(split, build_market_index(split))
        rows.sort(key=lambda r: int(r.get("start_time") or 0))
        if limit:
            rows = rows[-max(0, int(limit)) :]
        return [str(m["market_id"]) for m in rows]

    markets = list_markets(split, limit=limit)
    return [m["market_id"] for m in markets]


def run_backtest(
    *,
    strategy: str = "lgbm_edge",
    split: str = "validation",
    market_ids: list[str] | None = None,
    limit: int = 20,
    date: str | None = None,
    strategy_params: dict[str, Any] | None = None,
    starting_cash: float = 1000.0,
    shared_bankroll: bool = True,
) -> dict[str, Any]:
    ids = _resolve_market_ids(
        split=split,
        market_ids=market_ids,
        limit=limit,
        date=date,
    )

    portfolio = Portfolio(cash=starting_cash) if shared_bankroll else None
    strategy_obj = create_strategy(strategy, strategy_params)

    results: list[dict[str, Any]] = []
    total_pnl = 0.0
    total_fills = 0
    wins = 0
    opportunities_found = 0
    markets_with_opportunities = 0
    pairs_filled = 0
    net_edge_sum = 0.0
    net_edge_count = 0

    for mid in ids:
        if strategy_obj is not None and hasattr(strategy_obj, "reset"):
            strategy_obj.reset()

        r = run_market_backtest(
            mid,
            split=split,
            strategy_name=strategy,
            strategy_params=strategy_params,
            starting_cash=starting_cash,
            portfolio=portfolio,
            strategy=strategy_obj,
        )
        fills = [
            f
            for f in (r.get("fills") or [])
            if str(f.get("action") or "").upper() != "SETTLE"
        ]
        mstats = r.get("stats") or {}
        opportunities_found += int(mstats.get("opportunities_found") or 0)
        markets_with_opportunities += int(mstats.get("markets_with_opportunities") or 0)
        pairs_filled += int(mstats.get("pairs_filled") or 0)
        avg_net = mstats.get("avg_net_edge")
        if avg_net is not None and int(mstats.get("pairs_filled") or 0) > 0:
            n_pairs = int(mstats.get("pairs_filled") or 0)
            net_edge_sum += float(avg_net) * n_pairs
            net_edge_count += n_pairs

        results.append(
            {
                "market_id": r["market_id"],
                "winner": r["winner"],
                "pnl": r["pnl"],
                "n_fills": r["n_fills"],
                "ending_cash": r["ending_cash"],
                "payout": r.get("payout"),
                "fills": fills,
                "equity": r.get("equity") or [],
                "signals": r.get("signals") or [],
                "stats": mstats,
            }
        )
        total_pnl += float(r["pnl"])
        total_fills += int(r["n_fills"])
        if r["pnl"] > 0:
            wins += 1

    n = len(results) or 1
    equity_curve = []
    cum = 0.0
    for i, r in enumerate(results):
        cum += float(r["pnl"])
        equity_curve.append(
            {
                "i": i + 1,
                "market_id": r["market_id"],
                "pnl": float(r["pnl"]),
                "cum_pnl": cum,
            }
        )

    fill_rate = pairs_filled / opportunities_found if opportunities_found > 0 else None

    if shared_bankroll and portfolio is not None:
        ending_cash = portfolio.cash
        total_pnl = ending_cash - starting_cash
    else:
        ending_cash = None

    return {
        "strategy": strategy,
        "split": split,
        "date": date,
        "shared_bankroll": shared_bankroll,
        "n_markets": len(results),
        "total_pnl": total_pnl,
        "avg_pnl": total_pnl / n,
        "win_rate": wins / n,
        "total_fills": total_fills,
        "starting_cash": starting_cash,
        "ending_cash": ending_cash,
        "markets": results,
        "equity_curve": equity_curve,
        "params": strategy_params or {},
        "stats": {
            "opportunities_found": opportunities_found,
            "markets_with_opportunities": markets_with_opportunities,
            "pairs_filled": pairs_filled,
            "avg_net_edge": net_edge_sum / net_edge_count if net_edge_count else None,
            "fill_rate": fill_rate,
        },
    }
