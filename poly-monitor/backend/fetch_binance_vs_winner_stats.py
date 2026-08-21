"""Lightweight Binance-Δ vs Polymarket winner stats for BTC/ETH × 5m/15m/1h.

Fetches only:
  - Gamma closed events by series_id (paginated JSON, no orderbooks)
  - Binance klines for the covered window (1–2 requests per asset×interval)

30m series do not exist. Hourly uses series slugs btc/eth-up-or-down-hourly.

Usage:
  cd poly-monitor/backend
  python fetch_binance_vs_winner_stats.py
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parents[1] / ".cache" / "binance_vs_winner_btc_eth.json"
OUT_MARKETS = Path(__file__).resolve().parents[1] / ".cache" / "binance_vs_winner_btc_eth_markets.jsonl"
OUT_MARKETS_JSON = Path(__file__).resolve().parents[1] / ".cache" / "binance_vs_winner_btc_eth_markets.json"

SERIES = [
    # (label, series_slug, asset, tf, binance_symbol, kline_interval, duration_s, order_field)
    ("btc_5m", "btc-up-or-down-5m", "BTC", "5m", "BTCUSDT", "5m", 300, "startTime"),
    ("btc_15m", "btc-up-or-down-15m", "BTC", "15m", "BTCUSDT", "15m", 900, "startTime"),
    ("btc_1h", "btc-up-or-down-hourly", "BTC", "1h", "BTCUSDT", "1h", 3600, "id"),
    ("eth_5m", "eth-up-or-down-5m", "ETH", "5m", "ETHUSDT", "5m", 300, "startTime"),
    ("eth_15m", "eth-up-or-down-15m", "ETH", "15m", "ETHUSDT", "15m", 900, "startTime"),
    ("eth_1h", "eth-up-or-down-hourly", "ETH", "1h", "ETHUSDT", "1h", 3600, "id"),
]

# Look back this many days of closed markets (keeps Gamma pages small).
LOOKBACK_DAYS = 3
PAGE_LIMIT = 100
MAX_PAGES = 40  # safety cap per series
UA = "Mozilla/5.0 poly-monitor-stats/1.0"


def http_get_json(url: str, *, retries: int = 3) -> Any:
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.4 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def resolve_series_id(series_slug: str) -> int | None:
    data = http_get_json(
        "https://gamma-api.polymarket.com/series?"
        + urllib.parse.urlencode({"slug": series_slug})
    )
    if not data:
        return None
    row = data[0] if isinstance(data, list) else data
    try:
        return int(row["id"])
    except (KeyError, TypeError, ValueError):
        return None


def parse_winner(event: dict[str, Any]) -> int | None:
    """Return 1=UP, 0=DOWN from outcomePrices."""
    markets = event.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except json.JSONDecodeError:
            return None
    prices = m.get("outcomePrices")
    outcomes = m.get("outcomes") or ["Up", "Down"]
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            return None
    if not prices or len(prices) < 2:
        return None
    try:
        up_p = float(prices[0])
        down_p = float(prices[1])
    except (TypeError, ValueError):
        return None
    # Resolved markets are typically 1/0.
    if up_p >= 0.99 and down_p <= 0.01:
        return 1
    if down_p >= 0.99 and up_p <= 0.01:
        return 0
    # Fallback: higher settled side
    if up_p == down_p:
        return None
    return 1 if up_p > down_p else 0


def event_start_unix(event: dict[str, Any]) -> int | None:
    markets = event.get("markets") or []
    m: dict[str, Any] = {}
    if markets:
        raw = markets[0]
        if isinstance(raw, str):
            try:
                m = json.loads(raw)
            except json.JSONDecodeError:
                m = {}
        elif isinstance(raw, dict):
            m = raw
    for key in ("eventStartTime", "startTime"):
        est = m.get(key) or event.get(key)
        if est:
            try:
                dt = datetime.fromisoformat(str(est).replace("Z", "+00:00"))
                return int(dt.timestamp())
            except ValueError:
                pass
    slug = str(event.get("slug") or "")
    parts = slug.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return None


def fetch_closed_events(
    series_id: int, *, cutoff_unix: int, order_field: str = "startTime"
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    for _ in range(MAX_PAGES):
        q = urllib.parse.urlencode(
            {
                "series_id": series_id,
                "closed": "true",
                "limit": PAGE_LIMIT,
                "offset": offset,
                "order": order_field,
                "ascending": "false",
            }
        )
        page = http_get_json(f"https://gamma-api.polymarket.com/events?{q}")
        if not isinstance(page, list) or not page:
            break
        stop = False
        for ev in page:
            start = event_start_unix(ev)
            if start is not None and start < cutoff_unix:
                stop = True
                # still skip old rows; for order=id newest-first, later pages are older
                continue
            if not ev.get("closed"):
                continue
            winner = parse_winner(ev)
            if winner is None or start is None:
                continue
            meta = ev.get("eventMetadata") or {}
            out.append(
                {
                    "slug": ev.get("slug"),
                    "start_unix": start,
                    "winner": winner,
                    "price_to_beat": meta.get("priceToBeat"),
                    "final_price": meta.get("finalPrice"),
                }
            )
        if stop or len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        time.sleep(0.05)
    by_slug = {r["slug"]: r for r in out}
    return list(by_slug.values())


def fetch_binance_klines(
    symbol: str, interval: str, start_ms: int, end_ms: int
) -> dict[int, tuple[float, float]]:
    """Map open_time_ms -> (open, close)."""
    bases = (
        "https://data-api.binance.vision",
        "https://api.binance.us",
    )
    out: dict[int, tuple[float, float]] = {}
    cursor = start_ms
    while cursor < end_ms:
        q = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            }
        )
        rows = None
        last_err: Exception | None = None
        for base in bases:
            try:
                rows = http_get_json(f"{base}/api/v3/klines?{q}")
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        if rows is None:
            raise RuntimeError(f"binance klines failed: {last_err}")
        if not rows:
            break
        for row in rows:
            open_ms = int(row[0])
            o = float(row[1])
            c = float(row[4])
            out[open_ms] = (o, c)
        last_open = int(rows[-1][0])
        nxt = last_open + 1
        if nxt <= cursor:
            break
        cursor = nxt
        if len(rows) < 1000:
            break
        time.sleep(0.05)
    return out


def summarize(
    label: str,
    asset: str,
    tf: str,
    rows: list[dict[str, Any]],
    *,
    note: str = "",
) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {
            "label": label,
            "asset": asset,
            "tf": tf,
            "available": True,
            "n": 0,
            "pct_bn_match_winner": None,
            "mean_abs_bn_delta": None,
            "mean_abs_twap_delta": None,
            "pct_bn_twap_sign_disagree": None,
            "note": note or "no resolved markets in window",
        }
    bn_match = sum(1 for r in rows if r["bn_match_winner"])
    twap_rows = [r for r in rows if r.get("twap_delta") is not None]
    disagree = sum(
        1
        for r in twap_rows
        if (r["binance_delta"] >= 0) != (r["twap_delta"] >= 0)
    )
    return {
        "label": label,
        "asset": asset,
        "tf": tf,
        "available": True,
        "n": n,
        "bn_match_winner": bn_match,
        "pct_bn_match_winner": round(100 * bn_match / n, 2),
        "mean_abs_bn_delta": round(
            sum(abs(r["binance_delta"]) for r in rows) / n, 4
        ),
        "n_with_twap_meta": len(twap_rows),
        "mean_abs_twap_delta": (
            round(sum(abs(r["twap_delta"]) for r in twap_rows) / len(twap_rows), 4)
            if twap_rows
            else None
        ),
        "n_bn_twap_sign_disagree": disagree,
        "pct_bn_twap_sign_disagree": (
            round(100 * disagree / len(twap_rows), 2) if twap_rows else None
        ),
        "note": note,
    }


def main() -> None:
    now = int(time.time())
    cutoff = now - LOOKBACK_DAYS * 86_400
    print(f"lookback_days={LOOKBACK_DAYS} cutoff_unix={cutoff}")

    results: list[dict[str, Any]] = []
    samples: dict[str, list[dict[str, Any]]] = {}
    all_markets: list[dict[str, Any]] = []

    for label, series_slug, asset, tf, symbol, interval, dur, order_field in SERIES:
        print(f"\n=== {label} ===")
        sid = resolve_series_id(series_slug)
        if sid is None:
            results.append(
                {
                    "label": label,
                    "asset": asset,
                    "tf": tf,
                    "available": False,
                    "n": 0,
                    "note": f"series '{series_slug}' not found on Gamma",
                }
            )
            print("  missing series")
            continue

        events = fetch_closed_events(sid, cutoff_unix=cutoff, order_field=order_field)
        print(f"  series_id={sid} closed_events={len(events)}")
        if not events:
            results.append(
                summarize(label, asset, tf, [], note="no closed events fetched")
            )
            continue

        starts = [e["start_unix"] for e in events]
        start_ms = (min(starts) - dur) * 1000
        end_ms = (max(starts) + dur) * 1000
        klines = fetch_binance_klines(symbol, interval, start_ms, end_ms)
        print(f"  binance_klines={len(klines)}")

        rows: list[dict[str, Any]] = []
        missed_klines = 0
        for ev in events:
            open_ms = int(ev["start_unix"]) * 1000
            oc = klines.get(open_ms)
            if oc is None:
                missed_klines += 1
                continue
            bn_o, bn_c = oc
            bn_d = bn_c - bn_o
            winner = int(ev["winner"])
            win_sign = 1 if winner == 1 else -1
            bn_sign = 1 if bn_d >= 0 else -1
            twap_d = None
            ptb, fin = ev.get("price_to_beat"), ev.get("final_price")
            try:
                if ptb is not None and fin is not None:
                    twap_d = float(fin) - float(ptb)
            except (TypeError, ValueError):
                twap_d = None
            row = {
                "label": label,
                "asset": asset,
                "tf": tf,
                "series_slug": series_slug,
                "slug": ev["slug"],
                "start_unix": ev["start_unix"],
                "end_unix": int(ev["start_unix"]) + int(dur),
                "winner": "UP" if winner == 1 else "DOWN",
                "winner_int": winner,
                "binance_symbol": symbol,
                "binance_interval": interval,
                "binance_open": round(bn_o, 8),
                "binance_close": round(bn_c, 8),
                "binance_delta": round(bn_d, 8),
                "price_to_beat": ptb,
                "final_price": fin,
                "twap_delta": round(twap_d, 8) if twap_d is not None else None,
                "bn_match_winner": bn_sign == win_sign,
            }
            rows.append(row)
            all_markets.append(row)
        if missed_klines:
            print(f"  missed_klines={missed_klines}")

        stats = summarize(label, asset, tf, rows)
        results.append(stats)
        # keep a few disagreements for the report
        bad = [
            {
                "slug": r["slug"],
                "winner": r["winner"],
                "binance_delta": round(r["binance_delta"], 4),
                "twap_delta": (
                    round(r["twap_delta"], 4) if r["twap_delta"] is not None else None
                ),
            }
            for r in rows
            if not r["bn_match_winner"]
        ]
        bad.sort(key=lambda x: abs(x["binance_delta"]), reverse=True)
        samples[label] = bad[:12]
        print(
            f"  n={stats['n']} bn_match={stats['pct_bn_match_winner']}% "
            f"mean|bnD|={stats['mean_abs_bn_delta']}"
        )

    payload = {
        "generated_at_unix": now,
        "lookback_days": LOOKBACK_DAYS,
        "method": {
            "winner": "Gamma outcomePrices on closed events (series_id pages)",
            "binance_delta": "Binance kline close−open for window start (aligned)",
            "twap_delta": "eventMetadata.finalPrice − priceToBeat when present",
        },
        "files": {
            "summary": str(OUT),
            "markets_jsonl": str(OUT_MARKETS),
            "markets_json": str(OUT_MARKETS_JSON),
        },
        "results": results,
        "disagreement_samples": samples,
        "n_markets": len(all_markets),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    OUT_MARKETS_JSON.write_text(
        json.dumps(
            {
                "generated_at_unix": now,
                "lookback_days": LOOKBACK_DAYS,
                "method": payload["method"],
                "markets": all_markets,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    with OUT_MARKETS.open("w", encoding="utf-8") as f:
        for row in all_markets:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    print(f"\nwrote {OUT}")
    print(f"wrote {OUT_MARKETS} ({len(all_markets)} rows)")
    print(f"wrote {OUT_MARKETS_JSON}")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
