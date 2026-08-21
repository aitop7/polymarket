"""BTC hourly Up/Down winner vs Binance 1h Δ over ~3 months.

Hourly markets settle on Chainlink spot (not VWAP/TWAP). We still record
eventMetadata priceToBeat/finalPrice as chainlink open/close when present.

Usage:
  cd poly-monitor/backend
  python fetch_btc_1h_binance_vs_winner_3m.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

# Reuse Gamma/Binance helpers from the multi-series fetcher.
from fetch_binance_vs_winner_stats import (
    event_start_unix,
    fetch_binance_klines,
    http_get_json,
    parse_winner,
    resolve_series_id,
)
import fetch_binance_vs_winner_stats as base

LOOKBACK_DAYS = 90
PAGE_LIMIT = 100
MAX_PAGES = 50  # ~2160 hourly markets in 90d
SERIES_SLUG = "btc-up-or-down-hourly"
SYMBOL = "BTCUSDT"
INTERVAL = "1h"
DUR_S = 3600

OUT_SUMMARY = (
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "btc_1h_binance_vs_winner_3m.json"
)
OUT_MARKETS = (
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "btc_1h_binance_vs_winner_3m_markets.jsonl"
)
OUT_MARKETS_JSON = (
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "btc_1h_binance_vs_winner_3m_markets.json"
)


def fetch_closed_events(series_id: int, *, cutoff_unix: int) -> list[dict[str, Any]]:
    """Fetch closed hourly events in ~30d windows (avoids Gamma offset≈2000 cap)."""
    from datetime import datetime, timezone

    now = int(time.time())
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    window_s = 30 * 86_400
    window_end = now
    win_i = 0
    while window_end > cutoff_unix:
        win_i += 1
        window_start = max(cutoff_unix, window_end - window_s)
        end_date_max = datetime.fromtimestamp(window_end, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        end_date_min = datetime.fromtimestamp(window_start, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        print(f"  window={win_i} {end_date_min} .. {end_date_max}")
        offset = 0
        for page_i in range(MAX_PAGES):
            import urllib.parse

            params = {
                "series_id": series_id,
                "closed": "true",
                "limit": PAGE_LIMIT,
                "offset": offset,
                "order": "id",
                "ascending": "false",
                "end_date_min": end_date_min,
                "end_date_max": end_date_max,
            }
            q = urllib.parse.urlencode(params)
            try:
                page = http_get_json(f"https://gamma-api.polymarket.com/events?{q}")
            except RuntimeError as exc:
                print(f"    stop at offset={offset}: {exc}")
                break
            if not isinstance(page, list) or not page:
                break
            n_kept = 0
            for ev in page:
                start = event_start_unix(ev)
                if start is None or start < cutoff_unix:
                    continue
                if not ev.get("closed"):
                    continue
                winner = parse_winner(ev)
                if winner is None:
                    continue
                slug = str(ev.get("slug") or "")
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                meta = ev.get("eventMetadata") or {}
                out.append(
                    {
                        "slug": slug,
                        "start_unix": start,
                        "winner": winner,
                        "price_to_beat": meta.get("priceToBeat"),
                        "final_price": meta.get("finalPrice"),
                    }
                )
                n_kept += 1
            print(f"    page {page_i} offset={offset} kept={n_kept} total={len(out)}")
            if len(page) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
            time.sleep(0.05)
        window_end = window_start
        time.sleep(0.1)
    return out


def main() -> None:
    # Temporarily allow deeper paging if helpers share MAX_PAGES later.
    base.MAX_PAGES = MAX_PAGES
    base.PAGE_LIMIT = PAGE_LIMIT

    now = int(time.time())
    cutoff = now - LOOKBACK_DAYS * 86_400
    print(f"lookback_days={LOOKBACK_DAYS} cutoff_unix={cutoff}")

    sid = resolve_series_id(SERIES_SLUG)
    if sid is None:
        raise SystemExit(f"series not found: {SERIES_SLUG}")
    print(f"series_id={sid}")

    events = fetch_closed_events(sid, cutoff_unix=cutoff)
    print(f"closed_events={len(events)}")
    if not events:
        raise SystemExit("no events")

    starts = [e["start_unix"] for e in events]
    start_ms = (min(starts) - DUR_S) * 1000
    end_ms = (max(starts) + DUR_S) * 1000
    klines = fetch_binance_klines(SYMBOL, INTERVAL, start_ms, end_ms)
    print(f"binance_klines={len(klines)}")

    rows: list[dict[str, Any]] = []
    missed = 0
    for ev in events:
        open_ms = int(ev["start_unix"]) * 1000
        oc = klines.get(open_ms)
        if oc is None:
            missed += 1
            continue
        bn_o, bn_c = oc
        bn_d = bn_c - bn_o
        winner = int(ev["winner"])
        win_sign = 1 if winner == 1 else -1
        bn_sign = 1 if bn_d >= 0 else -1
        cl_d = None
        ptb, fin = ev.get("price_to_beat"), ev.get("final_price")
        try:
            if ptb is not None and fin is not None:
                cl_d = float(fin) - float(ptb)
        except (TypeError, ValueError):
            cl_d = None
        rows.append(
            {
                "slug": ev["slug"],
                "start_unix": ev["start_unix"],
                "end_unix": int(ev["start_unix"]) + DUR_S,
                "winner": "UP" if winner == 1 else "DOWN",
                "winner_int": winner,
                "binance_open": round(bn_o, 8),
                "binance_close": round(bn_c, 8),
                "binance_delta": round(bn_d, 8),
                "price_to_beat": ptb,
                "final_price": fin,
                "chainlink_delta": round(cl_d, 8) if cl_d is not None else None,
                "bn_match_winner": bn_sign == win_sign,
                "cl_match_winner": (
                    ((cl_d >= 0) == (winner == 1)) if cl_d is not None else None
                ),
                "bn_cl_sign_agree": (
                    ((bn_d >= 0) == (cl_d >= 0)) if cl_d is not None else None
                ),
            }
        )
    print(f"aligned_rows={len(rows)} missed_klines={missed}")

    n = len(rows)
    bn_match = sum(1 for r in rows if r["bn_match_winner"])
    cl_rows = [r for r in rows if r["chainlink_delta"] is not None]
    cl_match = sum(1 for r in cl_rows if r["cl_match_winner"])
    bn_cl_agree = sum(1 for r in cl_rows if r["bn_cl_sign_agree"])
    disagree = [r for r in rows if not r["bn_match_winner"]]
    disagree.sort(key=lambda r: abs(r["binance_delta"]), reverse=True)

    # Bucket by |binance_delta|
    buckets = [
        (0, 50, "<$50"),
        (50, 100, "$50–100"),
        (100, 250, "$100–250"),
        (250, 500, "$250–500"),
        (500, 1e18, "≥$500"),
    ]
    by_bucket = []
    for lo, hi, label in buckets:
        sub = [r for r in rows if lo <= abs(r["binance_delta"]) < hi]
        if not sub:
            by_bucket.append({"bucket": label, "n": 0})
            continue
        m = sum(1 for r in sub if r["bn_match_winner"])
        by_bucket.append(
            {
                "bucket": label,
                "n": len(sub),
                "bn_match_winner": m,
                "pct_bn_match_winner": round(100 * m / len(sub), 2),
            }
        )

    summary = {
        "generated_at_unix": now,
        "lookback_days": LOOKBACK_DAYS,
        "series_slug": SERIES_SLUG,
        "settlement": "Chainlink spot (hourly; not VWAP)",
        "method": {
            "winner": "Gamma outcomePrices on closed hourly events",
            "binance_delta": "Binance BTCUSDT 1h kline close−open at eventStartTime",
            "chainlink_delta": "eventMetadata.finalPrice − priceToBeat when present",
        },
        "n": n,
        "date_range": {
            "start_unix": min(r["start_unix"] for r in rows) if rows else None,
            "end_unix": max(r["start_unix"] for r in rows) if rows else None,
        },
        "bn_match_winner": bn_match,
        "pct_bn_match_winner": round(100 * bn_match / n, 2) if n else None,
        "mean_abs_bn_delta": round(sum(abs(r["binance_delta"]) for r in rows) / n, 4)
        if n
        else None,
        "n_with_chainlink_meta": len(cl_rows),
        "pct_cl_match_winner": (
            round(100 * cl_match / len(cl_rows), 2) if cl_rows else None
        ),
        "pct_bn_cl_sign_agree": (
            round(100 * bn_cl_agree / len(cl_rows), 2) if cl_rows else None
        ),
        "mean_abs_chainlink_delta": (
            round(sum(abs(r["chainlink_delta"]) for r in cl_rows) / len(cl_rows), 4)
            if cl_rows
            else None
        ),
        "by_abs_bn_delta_bucket": by_bucket,
        "bn_disagree_samples": [
            {
                "slug": r["slug"],
                "start_unix": r["start_unix"],
                "winner": r["winner"],
                "binance_delta": round(r["binance_delta"], 4),
                "chainlink_delta": (
                    round(r["chainlink_delta"], 4)
                    if r["chainlink_delta"] is not None
                    else None
                ),
            }
            for r in disagree[:20]
        ],
        "files": {
            "summary": str(OUT_SUMMARY),
            "markets_jsonl": str(OUT_MARKETS),
            "markets_json": str(OUT_MARKETS_JSON),
        },
    }

    OUT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    OUT_MARKETS_JSON.write_text(
        json.dumps(
            {
                "generated_at_unix": now,
                "lookback_days": LOOKBACK_DAYS,
                "settlement": summary["settlement"],
                "method": summary["method"],
                "markets": rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    with OUT_MARKETS.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")

    print(json.dumps({k: summary[k] for k in summary if k != "bn_disagree_samples"}, indent=2))
    print(f"\nwrote {OUT_SUMMARY}")
    print(f"wrote {OUT_MARKETS} ({n} rows)")


if __name__ == "__main__":
    main()
