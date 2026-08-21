"""Recalculate Binance-Δ vs winner stats from a local markets dump.

Reads one of:
  poly-monitor/.cache/binance_vs_winner_btc_eth_markets.json
  poly-monitor/.cache/binance_vs_winner_btc_eth_markets.jsonl

Usage:
  cd poly-monitor/backend
  python recalculate_binance_vs_winner.py
  python recalculate_binance_vs_winner.py --path ../.cache/binance_vs_winner_btc_eth_markets.jsonl
  python recalculate_binance_vs_winner.py --asset BTC --tf 1h
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_JSON = Path(__file__).resolve().parents[1] / ".cache" / "binance_vs_winner_btc_eth_markets.json"
DEFAULT_JSONL = Path(__file__).resolve().parents[1] / ".cache" / "binance_vs_winner_btc_eth_markets.jsonl"


def load_markets(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix == ".jsonl":
        rows: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("markets"), list):
        return data["markets"]
    raise ValueError(f"Unrecognized markets file shape: {path}")


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    bn_match = sum(1 for r in rows if r.get("bn_match_winner"))
    # recompute match if missing
    if any("bn_match_winner" not in r for r in rows):
        bn_match = 0
        for r in rows:
            bn_d = float(r["binance_delta"])
            win = r.get("winner_int")
            if win is None:
                win = 1 if str(r.get("winner")).upper() == "UP" else 0
            if (bn_d >= 0) == (int(win) == 1):
                bn_match += 1
    twap_rows = [r for r in rows if r.get("twap_delta") is not None]
    disagree = 0
    for r in twap_rows:
        if (float(r["binance_delta"]) >= 0) != (float(r["twap_delta"]) >= 0):
            disagree += 1
    return {
        "n": n,
        "bn_match_winner": bn_match,
        "pct_bn_match_winner": round(100 * bn_match / n, 2),
        "mean_abs_bn_delta": round(sum(abs(float(r["binance_delta"])) for r in rows) / n, 4),
        "n_with_twap_meta": len(twap_rows),
        "mean_abs_twap_delta": (
            round(sum(abs(float(r["twap_delta"])) for r in twap_rows) / len(twap_rows), 4)
            if twap_rows
            else None
        ),
        "n_bn_twap_sign_disagree": disagree,
        "pct_bn_twap_sign_disagree": (
            round(100 * disagree / len(twap_rows), 2) if twap_rows else None
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--path",
        type=Path,
        default=None,
        help="markets .json or .jsonl (default: .cache/..._markets.json then .jsonl)",
    )
    ap.add_argument("--asset", choices=["BTC", "ETH"], default=None)
    ap.add_argument("--tf", choices=["5m", "15m", "1h"], default=None)
    args = ap.parse_args()

    path = args.path
    if path is None:
        path = DEFAULT_JSON if DEFAULT_JSON.is_file() else DEFAULT_JSONL
    markets = load_markets(path)
    if args.asset:
        markets = [r for r in markets if str(r.get("asset")).upper() == args.asset]
    if args.tf:
        markets = [r for r in markets if str(r.get("tf")) == args.tf]

    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in markets:
        key = str(r.get("label") or f"{r.get('asset')}_{r.get('tf')}")
        by_label[key].append(r)

    out = {
        "source": str(path),
        "n_markets": len(markets),
        "filters": {"asset": args.asset, "tf": args.tf},
        "by_label": {k: summarize(v) for k, v in sorted(by_label.items())},
        "overall": summarize(markets),
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
