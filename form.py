"""Merge first/ + last/ partial captures into full market files at this directory."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
FIRST = ROOT / "first"
LAST = ROOT / "last"

SNAPSHOT_FILES = (
    "binance_price_orderbook.parquet",
    "chainlink_price.parquet",
    "orderbooks.parquet",
)
EVENT_FILES = (
    "binance_trades.parquet",
    "trades.parquet",
)


def _read(path: Path) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    return pd.read_parquet(path)


def merge_snapshots(name: str) -> pd.DataFrame:
    a = _read(FIRST / name)
    b = _read(LAST / name)
    if a.empty and b.empty:
        return pd.DataFrame()
    out = pd.concat([a, b], ignore_index=True)
    out = out.drop_duplicates(subset=["timestamp"], keep="last")
    return out.sort_values("timestamp").reset_index(drop=True)


def merge_events(name: str) -> pd.DataFrame:
    a = _read(FIRST / name)
    b = _read(LAST / name)
    if a.empty and b.empty:
        return pd.DataFrame()
    out = pd.concat([a, b], ignore_index=True)
    # trades.parquet may contain multiple identical Orbscan legs (same wallet /
    # price / shares); only fill_index distinguishes them — never drop those.
    if name == "trades.parquet" and "fill_index" in out.columns:
        subset = [
            c
            for c in (
                "timestamp",
                "transaction_hash",
                "wallet",
                "is_up",
                "is_buy",
                "is_taker",
                "price",
                "shares",
                "fill_index",
            )
            if c in out.columns
        ]
        out = out.drop_duplicates(subset=subset, keep="last")
    else:
        out = out.drop_duplicates(keep="last")
    if "timestamp" in out.columns:
        out = out.sort_values("timestamp")
    return out.reset_index(drop=True)


def merge_meta() -> dict:
    first = json.loads((FIRST / "meta.json").read_text(encoding="utf-8"))
    last = json.loads((LAST / "meta.json").read_text(encoding="utf-8"))
    meta = {**first, **last}
    if first.get("btc_open_price") is not None:
        meta["btc_open_price"] = first["btc_open_price"]
    if last.get("btc_close_price") is not None:
        meta["btc_close_price"] = last["btc_close_price"]
    for k in ("data_health", "data_health_checked_at", "data_health_comment"):
        meta.pop(k, None)
    return meta


def main() -> None:
    assert FIRST.is_dir() and LAST.is_dir(), "expected first/ and last/ subfolders"
    print("Merging into", ROOT)
    for name in SNAPSHOT_FILES:
        df = merge_snapshots(name)
        df.to_parquet(ROOT / name, index=False)
        print(f"  {name}: {len(df)} rows")
    for name in EVENT_FILES:
        df = merge_events(name)
        df.to_parquet(ROOT / name, index=False)
        print(f"  {name}: {len(df)} rows")
    meta = merge_meta()
    (ROOT / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print("  meta.json: open=", meta.get("btc_open_price"), "close=", meta.get("btc_close_price"))
    print("done")


if __name__ == "__main__":
    main()
