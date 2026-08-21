"""Compare BTC vs ETH Polymarket winners on aligned windows (local dump)."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

MARKETS = (
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "binance_vs_winner_btc_eth_markets.json"
)
OUT = (
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "btc_vs_eth_winner_compare.json"
)


def main() -> None:
    markets = json.loads(MARKETS.read_text(encoding="utf-8"))["markets"]
    by: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in markets:
        by[(r["tf"], int(r["start_unix"]))][r["asset"]] = r

    results: dict[str, Any] = {}
    for tf in ("5m", "15m", "1h"):
        pairs: list[dict[str, Any]] = []
        for (t, start), assets in by.items():
            if t != tf or "BTC" not in assets or "ETH" not in assets:
                continue
            b, e = assets["BTC"], assets["ETH"]
            pairs.append(
                {
                    "start_unix": start,
                    "btc_winner": b["winner"],
                    "eth_winner": e["winner"],
                    "same_winner": b["winner"] == e["winner"],
                    "btc_bn_delta": b["binance_delta"],
                    "eth_bn_delta": e["binance_delta"],
                    "same_bn_sign": (b["binance_delta"] >= 0)
                    == (e["binance_delta"] >= 0),
                    "btc_slug": b["slug"],
                    "eth_slug": e["slug"],
                }
            )

        n = len(pairs)
        same = sum(1 for p in pairs if p["same_winner"])
        uu = sum(1 for p in pairs if p["btc_winner"] == "UP" and p["eth_winner"] == "UP")
        ud = sum(1 for p in pairs if p["btc_winner"] == "UP" and p["eth_winner"] == "DOWN")
        du = sum(1 for p in pairs if p["btc_winner"] == "DOWN" and p["eth_winner"] == "UP")
        dd = sum(
            1 for p in pairs if p["btc_winner"] == "DOWN" and p["eth_winner"] == "DOWN"
        )
        btc_up = [p for p in pairs if p["btc_winner"] == "UP"]
        btc_dn = [p for p in pairs if p["btc_winner"] == "DOWN"]
        same_bn = sum(1 for p in pairs if p["same_bn_sign"])
        disagree = [p for p in pairs if not p["same_winner"]]
        disagree.sort(key=lambda p: abs(p["btc_bn_delta"]), reverse=True)

        results[tf] = {
            "n_paired": n,
            "same_winner": same,
            "pct_same_winner": round(100 * same / n, 2) if n else None,
            "disagree": n - same,
            "pct_disagree": round(100 * (n - same) / n, 2) if n else None,
            "contingency_btc_eth": {
                "UP_UP": uu,
                "UP_DOWN": ud,
                "DOWN_UP": du,
                "DOWN_DOWN": dd,
            },
            "P_ETH_UP_given_BTC_UP": (
                round(100 * sum(1 for p in btc_up if p["eth_winner"] == "UP") / len(btc_up), 2)
                if btc_up
                else None
            ),
            "n_BTC_UP": len(btc_up),
            "P_ETH_DOWN_given_BTC_DOWN": (
                round(
                    100
                    * sum(1 for p in btc_dn if p["eth_winner"] == "DOWN")
                    / len(btc_dn),
                    2,
                )
                if btc_dn
                else None
            ),
            "n_BTC_DOWN": len(btc_dn),
            "pct_same_binance_sign": round(100 * same_bn / n, 2) if n else None,
            "disagree_samples": [
                {
                    "start_unix": p["start_unix"],
                    "btc": p["btc_winner"],
                    "eth": p["eth_winner"],
                    "btc_bn_delta": round(p["btc_bn_delta"], 4),
                    "eth_bn_delta": round(p["eth_bn_delta"], 4),
                }
                for p in disagree[:12]
            ],
        }

    payload = {
        "source": str(MARKETS),
        "method": "Pair BTC/ETH by identical start_unix within each tf; compare Gamma winners",
        "by_tf": results,
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
