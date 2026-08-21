"""BNB 15m Up/Down winner vs Binance BNBUSDT 15m Δ (from date → today).

Usage:
  cd poly-monitor/backend
  python fetch_bnb_15m_binance_vs_winner.py
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

# Inclusive start: 2026-08-07 00:00 UTC
DATE_FROM = datetime(2026, 8, 7, 0, 0, 0, tzinfo=timezone.utc)
SERIES_SLUG = "bnb-up-or-down-15m"
SYMBOL = "BNBUSDT"
INTERVAL = "15m"
DUR_S = 900
PAGE_LIMIT = 100
MAX_PAGES = 40
WINDOW_S = 7 * 86_400

OUT_SUMMARY = (
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "bnb_15m_binance_vs_winner_aug7.json"
)
OUT_MARKETS = (
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "bnb_15m_binance_vs_winner_aug7_markets.jsonl"
)
OUT_MARKETS_JSON = (
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "bnb_15m_binance_vs_winner_aug7_markets.json"
)


def fetch_closed_events(series_id: int, *, cutoff_unix: int) -> list[dict[str, Any]]:
    now = int(time.time())
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    window_end = now
    win_i = 0
    while window_end > cutoff_unix:
        win_i += 1
        window_start = max(cutoff_unix, window_end - WINDOW_S)
        end_date_max = datetime.fromtimestamp(window_end, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        end_date_min = datetime.fromtimestamp(window_start, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        print(f"  window={win_i} {end_date_min} .. {end_date_max}")
        offset = 0
        for page_i in range(MAX_PAGES):
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
    now = int(time.time())
    cutoff = int(DATE_FROM.timestamp())
    print(
        f"from={DATE_FROM.isoformat()} cutoff_unix={cutoff} now={now} "
        f"(~{(now - cutoff) / 86400:.1f}d)"
    )

    sid = resolve_series_id(SERIES_SLUG)
    if sid is None:
        raise SystemExit(f"series not found: {SERIES_SLUG}")
    print(f"series_id={sid}")

    events = fetch_closed_events(sid, cutoff_unix=cutoff)
    print(f"closed_events={len(events)}")
    if not events:
        raise SystemExit("no events")

    starts = [e["start_unix"] for e in events]
    klines = fetch_binance_klines(
        SYMBOL, INTERVAL, (min(starts) - DUR_S) * 1000, (max(starts) + DUR_S) * 1000
    )
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
        twap_d = None
        ptb, fin = ev.get("price_to_beat"), ev.get("final_price")
        try:
            if ptb is not None and fin is not None:
                twap_d = float(fin) - float(ptb)
        except (TypeError, ValueError):
            twap_d = None
        rows.append(
            {
                "slug": ev["slug"],
                "start_unix": ev["start_unix"],
                "end_unix": int(ev["start_unix"]) + DUR_S,
                "winner": "UP" if winner == 1 else "DOWN",
                "winner_int": winner,
                "binance_symbol": SYMBOL,
                "binance_open": round(bn_o, 8),
                "binance_close": round(bn_c, 8),
                "binance_delta": round(bn_d, 8),
                "price_to_beat": ptb,
                "final_price": fin,
                "meta_delta": round(twap_d, 8) if twap_d is not None else None,
                "bn_match_winner": (bn_d >= 0) == (winner == 1),
                "meta_match_winner": (
                    ((twap_d >= 0) == (winner == 1)) if twap_d is not None else None
                ),
                "bn_meta_sign_agree": (
                    ((bn_d >= 0) == (twap_d >= 0)) if twap_d is not None else None
                ),
            }
        )
    print(f"aligned_rows={len(rows)} missed_klines={missed}")

    n = len(rows)
    bn_match = sum(1 for r in rows if r["bn_match_winner"])
    meta_rows = [r for r in rows if r["meta_delta"] is not None]
    meta_match = sum(1 for r in meta_rows if r["meta_match_winner"])
    bn_meta = sum(1 for r in meta_rows if r["bn_meta_sign_agree"])
    disagree = [r for r in rows if not r["bn_match_winner"]]
    disagree.sort(key=lambda r: abs(r["binance_delta"]), reverse=True)

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
                "bn_match_winner": m,
                "pct_bn_match_winner": round(100 * m / len(sub), 2),
            }
        )

    summary = {
        "generated_at_unix": now,
        "date_from": DATE_FROM.isoformat(),
        "series_slug": SERIES_SLUG,
        "binance_symbol": SYMBOL,
        "interval": INTERVAL,
        "method": {
            "winner": "Gamma outcomePrices on closed events",
            "binance_delta": "Binance BNBUSDT 15m kline close−open at eventStartTime",
            "meta_delta": "eventMetadata.finalPrice − priceToBeat when present",
        },
        "n": n,
        "missed_klines": missed,
        "date_range": {
            "start_unix": min(r["start_unix"] for r in rows) if rows else None,
            "end_unix": max(r["start_unix"] for r in rows) if rows else None,
        },
        "bn_match_winner": bn_match,
        "pct_bn_match_winner": round(100 * bn_match / n, 2) if n else None,
        "mean_abs_bn_delta": round(sum(abs(r["binance_delta"]) for r in rows) / n, 6)
        if n
        else None,
        "n_with_meta": len(meta_rows),
        "pct_meta_match_winner": (
            round(100 * meta_match / len(meta_rows), 2) if meta_rows else None
        ),
        "pct_bn_meta_sign_agree": (
            round(100 * bn_meta / len(meta_rows), 2) if meta_rows else None
        ),
        "by_abs_bn_delta_bucket": by_bucket,
        "bn_disagree_samples": [
            {
                "slug": r["slug"],
                "start_unix": r["start_unix"],
                "winner": r["winner"],
                "binance_delta": round(r["binance_delta"], 6),
                "meta_delta": (
                    round(r["meta_delta"], 6) if r["meta_delta"] is not None else None
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
                "date_from": DATE_FROM.isoformat(),
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

    print(
        json.dumps(
            {k: summary[k] for k in summary if k != "bn_disagree_samples"},
            indent=2,
        )
    )
    print(f"\ndisagree n={len(disagree)}")
    for r in disagree[:10]:
        print(
            f"  {r['slug']} winner={r['winner']} bnD={r['binance_delta']:+.4f} "
            f"metaD={r['meta_delta']}"
        )
    print(f"\nwrote {OUT_SUMMARY}")
    print(f"wrote {OUT_MARKETS} ({n} rows)")


if __name__ == "__main__":
    main()
