"""Multi-market backtest runner."""

from __future__ import annotations

from typing import Any

from app.core.data import TWAP_SPLIT, list_markets
from app.core.market_index import (
    build_market_index,
    filter_history_markets,
)
from app.core.series import filter_rows_by_series, get_series
from app.engine.portfolio import Portfolio
from app.engine.replay import run_market_backtest
from app.strategies.registry import create_strategy

# Safety cap when resolving by date range (5m ≈ 288/day).
_MAX_RANGE_MARKETS = 5_000


def _normalize_date_range(
    date: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str | None, str | None]:
    """Resolve inclusive ET date bounds. Legacy `date` means from=to=date."""
    d0 = (date_from or date or "").strip() or None
    d1 = (date_to or date_from or date or "").strip() or None
    if d0 and d1 and d0 > d1:
        d0, d1 = d1, d0
    return d0, d1


def _resolve_market_ids(
    *,
    split: str,
    market_ids: list[str] | None,
    limit: int | None,
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    series: str = "5m",
) -> list[str]:
    series_key = get_series(series).key
    if market_ids:
        return [str(m) for m in market_ids]

    d0, d1 = _normalize_date_range(date, date_from, date_to)

    if d0 or d1:
        rows = filter_history_markets(split, build_market_index(split))
        rows = filter_rows_by_series(rows, series_key)
        if d0:
            rows = [r for r in rows if str(r.get("date_et") or "") >= d0]
        if d1:
            rows = [r for r in rows if str(r.get("date_et") or "") <= d1]
        rows.sort(key=lambda r: int(r.get("start_time") or 0))
        if len(rows) > _MAX_RANGE_MARKETS:
            rows = rows[:_MAX_RANGE_MARKETS]
        return [str(m["market_id"]) for m in rows]

    # No date range: fall back to limited recent / split scan.
    lim = max(1, int(limit or 20))
    if split == TWAP_SPLIT:
        rows = filter_history_markets(split, build_market_index(split))
        rows = filter_rows_by_series(rows, series_key)
        rows.sort(key=lambda r: int(r.get("start_time") or 0))
        rows = rows[-lim:]
        return [str(m["market_id"]) for m in rows]

    markets = list_markets(split, limit=lim)
    markets = filter_rows_by_series(markets, series_key)
    return [m["market_id"] for m in markets]


def run_backtest(
    *,
    strategy: str = "lgbm_edge",
    split: str = "validation",
    market_ids: list[str] | None = None,
    limit: int | None = 20,
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    series: str = "5m",
    strategy_params: dict[str, Any] | None = None,
    starting_cash: float = 1000.0,
    shared_bankroll: bool = True,
) -> dict[str, Any]:
    series_key = get_series(series).key
    d0, d1 = _normalize_date_range(date, date_from, date_to)
    ids = _resolve_market_ids(
        split=split,
        market_ids=market_ids,
        limit=limit,
        date=date,
        date_from=date_from,
        date_to=date_to,
        series=series_key,
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
        "series": series_key,
        "date": date if date and not date_from and not date_to else None,
        "date_from": d0,
        "date_to": d1,
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
