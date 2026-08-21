"""Compare BTC / ETH / BNB 15m winners vs their Binance Δ (same date window).

Default: 2026-08-07 UTC → now.

Usage:
  cd poly-monitor/backend
  python compare_15m_btc_eth_bnb_binance.py
"""

from __future__ import annotations

import json
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fetch_binance_vs_winner_stats import (
    event_start_unix,
    fetch_binance_klines,
    http_get_json,
    parse_winner,
    resolve_series_id,
)

DATE_FROM = datetime(2026, 8, 7, 0, 0, 0, tzinfo=timezone.utc)
DUR_S = 900
PAGE_LIMIT = 100
MAX_PAGES = 40
WINDOW_S = 7 * 86_400

SERIES = [
    ("btc_15m", "btc-up-or-down-15m", "BTC", "BTCUSDT"),
    ("eth_15m", "eth-up-or-down-15m", "ETH", "ETHUSDT"),
    ("bnb_15m", "bnb-up-or-down-15m", "BNB", "BNBUSDT"),
]

OUT = (
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "compare_15m_btc_eth_bnb_binance.json"
)
OUT_MARKETS = (
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "compare_15m_btc_eth_bnb_binance_markets.jsonl"
)


def fetch_closed_events(series_id: int, *, cutoff_unix: int) -> list[dict[str, Any]]:
    now = int(time.time())
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    window_end = now
    while window_end > cutoff_unix:
        window_start = max(cutoff_unix, window_end - WINDOW_S)
        end_date_max = datetime.fromtimestamp(window_end, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        end_date_min = datetime.fromtimestamp(window_start, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        offset = 0
        for _ in range(MAX_PAGES):
            q = urllib.parse.urlencode(
                {
                    "series_id": series_id,
                    "closed": "true",
                    "limit": PAGE_LIMIT,
                    "offset": offset,
                    "order": "startTime",
                    "ascending": "false",
                    "end_date_min": end_date_min,
                    "end_date_max": end_date_max,
                }
            )
            try:
                page = http_get_json(f"https://gamma-api.polymarket.com/events?{q}")
            except RuntimeError:
                break
            if not isinstance(page, list) or not page:
                break
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
            if len(page) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
            time.sleep(0.05)
        window_end = window_start
        time.sleep(0.08)
    return out


def summarize(rows: list[dict[str, Any]], *, asset: str) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"asset": asset, "n": 0}
    bn_match = sum(1 for r in rows if r["bn_match_winner"])
    meta = [r for r in rows if r.get("meta_delta") is not None]
    meta_match = sum(1 for r in meta if r.get("meta_match_winner"))
    bn_meta = sum(1 for r in meta if r.get("bn_meta_sign_agree"))
    # asset-specific |delta| buckets
    if asset == "BTC":
        buckets = [
            (0, 50, "<$50"),
            (50, 100, "$50–100"),
            (100, 250, "$100–250"),
            (250, 500, "$250–500"),
            (500, 1e18, "≥$500"),
        ]
    elif asset == "ETH":
        buckets = [
            (0, 2, "<$2"),
            (2, 5, "$2–5"),
            (5, 10, "$5–10"),
            (10, 20, "$10–20"),
            (20, 1e18, "≥$20"),
        ]
    else:
        buckets = [
            (0, 0.2, "<$0.20"),
            (0.2, 0.5, "$0.20–0.50"),
            (0.5, 1.0, "$0.50–1"),
            (1.0, 2.0, "$1–2"),
            (2.0, 1e18, "≥$2"),
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
                "pct_bn_match_winner": round(100 * m / len(sub), 2),
            }
        )
    disagree = [r for r in rows if not r["bn_match_winner"]]
    disagree.sort(key=lambda r: abs(r["binance_delta"]), reverse=True)
    return {
        "asset": asset,
        "n": n,
        "bn_match_winner": bn_match,
        "pct_bn_match_winner": round(100 * bn_match / n, 2),
        "disagree": n - bn_match,
        "pct_disagree": round(100 * (n - bn_match) / n, 2),
        "mean_abs_bn_delta": round(sum(abs(r["binance_delta"]) for r in rows) / n, 6),
        "n_with_meta": len(meta),
        "pct_meta_match_winner": (
            round(100 * meta_match / len(meta), 2) if meta else None
        ),
        "pct_bn_meta_sign_agree": (
            round(100 * bn_meta / len(meta), 2) if meta else None
        ),
        "by_abs_bn_delta_bucket": by_bucket,
        "disagree_samples": [
            {
                "slug": r["slug"],
                "winner": r["winner"],
                "binance_delta": round(r["binance_delta"], 6),
                "meta_delta": (
                    round(r["meta_delta"], 6) if r.get("meta_delta") is not None else None
                ),
            }
            for r in disagree[:10]
        ],
    }


def main() -> None:
    now = int(time.time())
    cutoff = int(DATE_FROM.timestamp())
    print(f"from={DATE_FROM.isoformat()} (~{(now - cutoff) / 86400:.1f}d)")

    all_rows: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for label, series_slug, asset, symbol in SERIES:
        print(f"\n=== {label} ===")
        sid = resolve_series_id(series_slug)
        if sid is None:
            results.append({"label": label, "asset": asset, "available": False})
            print("  missing series")
            continue
        events = fetch_closed_events(sid, cutoff_unix=cutoff)
        print(f"  series_id={sid} events={len(events)}")
        if not events:
            results.append(summarize([], asset=asset) | {"label": label, "available": True})
            continue
        starts = [e["start_unix"] for e in events]
        klines = fetch_binance_klines(
            symbol, "15m", (min(starts) - DUR_S) * 1000, (max(starts) + DUR_S) * 1000
        )
        print(f"  binance_klines={len(klines)}")
        rows: list[dict[str, Any]] = []
        missed = 0
        for ev in events:
            oc = klines.get(int(ev["start_unix"]) * 1000)
            if oc is None:
                missed += 1
                continue
            bn_o, bn_c = oc
            bn_d = bn_c - bn_o
            winner = int(ev["winner"])
            meta_d = None
            ptb, fin = ev.get("price_to_beat"), ev.get("final_price")
            try:
                if ptb is not None and fin is not None:
                    meta_d = float(fin) - float(ptb)
            except (TypeError, ValueError):
                meta_d = None
            row = {
                "label": label,
                "asset": asset,
                "slug": ev["slug"],
                "start_unix": ev["start_unix"],
                "winner": "UP" if winner == 1 else "DOWN",
                "winner_int": winner,
                "binance_symbol": symbol,
                "binance_open": round(bn_o, 8),
                "binance_close": round(bn_c, 8),
                "binance_delta": round(bn_d, 8),
                "price_to_beat": ptb,
                "final_price": fin,
                "meta_delta": round(meta_d, 8) if meta_d is not None else None,
                "bn_match_winner": (bn_d >= 0) == (winner == 1),
                "meta_match_winner": (
                    ((meta_d >= 0) == (winner == 1)) if meta_d is not None else None
                ),
                "bn_meta_sign_agree": (
                    ((bn_d >= 0) == (meta_d >= 0)) if meta_d is not None else None
                ),
            }
            rows.append(row)
            all_rows.append(row)
        print(f"  aligned={len(rows)} missed={missed}")
        stats = summarize(rows, asset=asset)
        stats["label"] = label
        stats["available"] = True
        stats["missed_klines"] = missed
        results.append(stats)
        print(
            f"  n={stats['n']} bn_match={stats['pct_bn_match_winner']}% "
            f"disagree={stats['disagree']}"
        )

    payload = {
        "generated_at_unix": now,
        "date_from": DATE_FROM.isoformat(),
        "tf": "15m",
        "method": {
            "winner": "Gamma outcomePrices on closed 15m events",
            "binance_delta": "Binance {BTC,ETH,BNB}USDT 15m close−open at eventStartTime",
            "meta_delta": "eventMetadata.finalPrice − priceToBeat when present",
        },
        "results": results,
        "n_markets_total": len(all_rows),
        "files": {"summary": str(OUT), "markets_jsonl": str(OUT_MARKETS)},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with OUT_MARKETS.open("w", encoding="utf-8") as f:
        for r in all_rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    print("\n" + json.dumps(results, indent=2))
    print(f"\nwrote {OUT}")
    print(f"wrote {OUT_MARKETS} ({len(all_rows)} rows)")


if __name__ == "__main__":
    main()
