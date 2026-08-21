"""Compare Binance mid delta vs Chainlink TWAP delta for 15m BTC Up/Down markets.

For each resolved market:
  binance_delta = Binance_BTC(close) - Binance_BTC(open)   # first/last in window
  twap_delta    = meta.btc_close_price - meta.btc_open_price  # official PTB / settlement

Winner is UP iff TWAP close >= TWAP open (meta), so twap_delta sign always matches winner.
Binance can disagree when spot mid moves differently from the 60s Chainlink TWAP.

Usage:
  cd poly-monitor/backend
  python -m app.scripts.binance_vs_twap_15m
  # or: python analyze_binance_vs_twap_15m.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.live_dataset import find_live_market_dir
from app.core.market_index import build_market_index, filter_history_markets
from app.core.series import filter_rows_by_series

OUT_PATH = Path(__file__).resolve().parents[1] / ".cache" / "binance_vs_twap_15m.json"


def _first_last(
    df: pd.DataFrame | None, col: str, start: int, end: int
) -> tuple[float | None, float | None]:
    if df is None or df.empty or col not in df.columns or "timestamp" not in df.columns:
        return None, None
    t = pd.to_numeric(df["timestamp"], errors="coerce")
    v = pd.to_numeric(df[col], errors="coerce")
    m = t.notna() & v.notna()
    inw = m & (t >= start) & (t <= end)
    use = df.loc[inw] if bool(inw.any()) else df.loc[m]
    if use.empty:
        return None, None
    use = use.sort_values("timestamp")
    return float(use.iloc[0][col]), float(use.iloc[-1][col])


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def analyze_15m() -> dict[str, Any]:
    rows = filter_rows_by_series(
        filter_history_markets("twap", build_market_index("twap")), "15m"
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        mid = str(r["market_id"])
        d = find_live_market_dir(mid)
        if d is None:
            continue
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        start = int(meta.get("start_time") or 0)
        end = int(meta.get("end_time") or 0)
        w = meta.get("winner")
        if w is True:
            winner = 1
        elif w is False:
            winner = 0
        else:
            continue

        bn_path = d / "binance_price_orderbook.parquet"
        bn = pd.read_parquet(bn_path) if bn_path.is_file() else None
        bn_o, bn_c = _first_last(bn, "Binance_BTC", start, end)
        meta_o = _as_float(meta.get("btc_open_price"))
        meta_c = _as_float(meta.get("btc_close_price"))
        if None in (bn_o, bn_c, meta_o, meta_c):
            continue

        bn_d = bn_c - bn_o
        cl_d = meta_c - meta_o
        bn_sign = 1 if bn_d >= 0 else -1
        cl_sign = 1 if cl_d >= 0 else -1
        win_sign = 1 if winner == 1 else -1
        out.append(
            {
                "market_id": mid,
                "slug": meta.get("slug") or "",
                "date_et": r.get("date_et") or "",
                "start_time": start,
                "winner": "UP" if winner == 1 else "DOWN",
                "binance_open": round(bn_o, 4),
                "binance_close": round(bn_c, 4),
                "binance_delta": round(bn_d, 4),
                "twap_open": round(meta_o, 4),
                "twap_close": round(meta_c, 4),
                "twap_delta": round(cl_d, 4),
                "binance_matches_winner": bn_sign == win_sign,
                "twap_matches_winner": cl_sign == win_sign,
                "signs_agree": bn_sign == cl_sign,
                "delta_diff": round(bn_d - cl_d, 4),
            }
        )

    n = len(out) or 1
    disagree = [x for x in out if not x["signs_agree"]]
    disagree.sort(key=lambda x: abs(float(x["delta_diff"])), reverse=True)
    buckets = {"0-5": 0, "5-15": 0, "15-30": 0, "30-60": 0, "60+": 0}
    for x in out:
        dlt = abs(float(x["binance_delta"]) - float(x["twap_delta"]))
        if dlt < 5:
            buckets["0-5"] += 1
        elif dlt < 15:
            buckets["5-15"] += 1
        elif dlt < 30:
            buckets["15-30"] += 1
        elif dlt < 60:
            buckets["30-60"] += 1
        else:
            buckets["60+"] += 1

    summary = {
        "n": len(out),
        "n_disagree_sign": len(disagree),
        "pct_disagree_sign": round(100 * len(disagree) / n, 2),
        "bn_match_winner": sum(1 for x in out if x["binance_matches_winner"]),
        "twap_match_winner": sum(1 for x in out if x["twap_matches_winner"]),
        "pct_bn_match_winner": round(
            100 * sum(1 for x in out if x["binance_matches_winner"]) / n, 2
        ),
        "pct_twap_match_winner": round(
            100 * sum(1 for x in out if x["twap_matches_winner"]) / n, 2
        ),
        "mean_abs_bn_delta": round(sum(abs(x["binance_delta"]) for x in out) / n, 2),
        "mean_abs_twap_delta": round(sum(abs(x["twap_delta"]) for x in out) / n, 2),
        "mean_abs_diff_deltas": round(
            sum(abs(x["binance_delta"] - x["twap_delta"]) for x in out) / n, 2
        ),
    }
    return {
        "summary": summary,
        "diff_buckets": buckets,
        "disagree": disagree,
        "markets": out,
    }


def main() -> None:
    payload = analyze_15m()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Full markets can be large; keep disagree + summary for tools, markets optional.
    slim = {
        "summary": payload["summary"],
        "diff_buckets": payload["diff_buckets"],
        "disagree": payload["disagree"],
        "n_markets": len(payload["markets"]),
    }
    OUT_PATH.write_text(json.dumps(slim, indent=2), encoding="utf-8")
    full = OUT_PATH.with_name("binance_vs_twap_15m_full.json")
    full.write_text(json.dumps(payload["markets"]), encoding="utf-8")
    s = payload["summary"]
    print(json.dumps(s, indent=2))
    print(f"wrote {OUT_PATH} and {full} ({s['n']} markets, {s['n_disagree_sign']} sign disagreements)")


if __name__ == "__main__":
    main()
