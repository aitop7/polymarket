"""Materialize live prediction features to parquet for faster train/eval.

Reads fetch_live market dirs (E:\\DataSets\\poly\\live\\…), runs the same
`live_features.engineer_features` path used by predict_up / direction / beta,
and writes one parquet per market under fetch_real/features_live/.

Usage (from poly-monitor/backend):

  python -m app.ml.build_live_features
  python -m app.ml.build_live_features --max-markets 200 --workers 8
  python -m app.ml.build_live_features --split chronological --train-ratio 0.8 --val-ratio 0.1
  python -m app.ml.build_live_features --closed-only --skip-existing

Layout (flat, default):
  features_live/{market_id}.parquet
  features_live/manifest.json

Layout (--split chronological):
  features_live/{train,validation,test}/{market_id}.parquet
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.config import settings
from app.core.data import DIRECTION_FEATURE_COLUMNS, FEATURE_COLUMNS
from app.core.live_dataset import iter_live_market_metas, live_data_root
from app.ml.live_features import load_live_feature_frame

SCHEMA_VERSION = 1

# Identity / label columns kept alongside engineered features.
ID_COLUMNS = (
    "timestamp",
    "market_id",
    "start_time",
    "end_time",
    "winner",
    "up_price",
    "down_price",
    "btc_price",
    "btc_chainlink",
    "btc_twap_30s",
)

# Preserve order; FEATURE_COLUMNS already includes up_mid / down_mid.
OUTPUT_COLUMNS: tuple[str, ...] = tuple(
    dict.fromkeys((*ID_COLUMNS, *FEATURE_COLUMNS, *DIRECTION_FEATURE_COLUMNS))
)


def _closed_markets(*, max_markets: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for meta in iter_live_market_metas():
        if not meta.get("closed") and meta.get("winner") is None:
            continue
        st = meta.get("start_time")
        if st is None:
            continue
        rows.append(
            {
                "market_id": str(meta["market_id"]),
                "dir": meta.get("dir"),
                "start_time": int(st),
                "end_time": int(meta["end_time"]) if meta.get("end_time") else None,
                "winner": meta.get("winner"),
                "date_utc": meta.get("date_utc"),
                "data_health": meta.get("data_health"),
            }
        )
    rows.sort(key=lambda r: (int(r["start_time"]), str(r["market_id"])))
    if max_markets is not None and max_markets > 0:
        rows = rows[-int(max_markets) :]
    return rows


def _assign_splits(
    markets: list[dict[str, Any]],
    *,
    train_ratio: float,
    val_ratio: float,
) -> list[dict[str, Any]]:
    """Chronological whole-market split → train / validation / test."""
    n = len(markets)
    if n == 0:
        return []
    train_ratio = min(max(float(train_ratio), 0.05), 0.95)
    val_ratio = min(max(float(val_ratio), 0.0), 0.45)
    if train_ratio + val_ratio >= 0.99:
        val_ratio = max(0.0, 0.99 - train_ratio)
    n_train = max(1, int(round(n * train_ratio)))
    n_val = int(round(n * val_ratio)) if val_ratio > 0 else 0
    if n_train + n_val >= n:
        n_val = max(0, n - n_train - 1)
    n_test = n - n_train - n_val
    out: list[dict[str, Any]] = []
    for i, m in enumerate(markets):
        if i < n_train:
            split = "train"
        elif i < n_train + n_val:
            split = "validation"
        else:
            split = "test"
        out.append({**m, "split": split})
    # Ensure test gets at least one market when possible.
    if n_test == 0 and n >= 3 and out:
        out[-1] = {**out[-1], "split": "test"}
    return out


def _select_output_frame(df: pd.DataFrame, market: dict[str, Any]) -> pd.DataFrame:
    out = df.copy()
    if "market_id" not in out.columns or out["market_id"].isna().all():
        out["market_id"] = str(market["market_id"])
    if "start_time" not in out.columns or out["start_time"].isna().all():
        if market.get("start_time") is not None:
            out["start_time"] = int(market["start_time"])
    if "end_time" not in out.columns or out["end_time"].isna().all():
        if market.get("end_time") is not None:
            out["end_time"] = int(market["end_time"])
    if "winner" not in out.columns:
        out["winner"] = market.get("winner")

    cols: list[str] = []
    for col in OUTPUT_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA
        cols.append(col)
    return out[cols]


def build_one_market(
    market: dict[str, Any],
    *,
    output_root: Path,
    compression: str,
    skip_existing: bool,
    min_rows: int,
) -> dict[str, Any]:
    mid = str(market["market_id"])
    split = market.get("split")
    dst = (
        output_root / str(split) / f"{mid}.parquet"
        if split
        else output_root / f"{mid}.parquet"
    )
    if skip_existing and dst.is_file() and dst.stat().st_size > 0:
        try:
            rows = int(pd.read_parquet(dst, columns=["timestamp"]).shape[0])
        except Exception:
            rows = 0
        return {
            "ok": True,
            "skipped": True,
            "market_id": mid,
            "split": split,
            "dst": str(dst),
            "rows": rows,
        }

    t0 = time.perf_counter()
    try:
        df = load_live_feature_frame(mid, market_dir=market.get("dir"))
    except Exception as exc:
        return {"ok": False, "market_id": mid, "split": split, "reason": f"load:{exc}"}

    if df is None or df.empty or "timestamp" not in df.columns:
        return {"ok": False, "market_id": mid, "split": split, "reason": "empty"}

    frame = _select_output_frame(df, market)
    if len(frame) < int(min_rows):
        return {
            "ok": False,
            "market_id": mid,
            "split": split,
            "reason": f"too_few_rows:{len(frame)}",
        }

    dst.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(dst, index=False, compression=compression)
    return {
        "ok": True,
        "skipped": False,
        "market_id": mid,
        "split": split,
        "dst": str(dst),
        "rows": int(len(frame)),
        "secs": round(time.perf_counter() - t0, 3),
    }


def build_live_features(
    *,
    output_root: Path | None = None,
    max_markets: int | None = None,
    workers: int = 4,
    compression: str = "zstd",
    skip_existing: bool = False,
    min_rows: int = 30,
    split_mode: str = "flat",
    train_ratio: float = 0.8,
    val_ratio: float = 0.1,
) -> dict[str, Any]:
    output_root = Path(output_root) if output_root else settings.features_live_dir
    output_root.mkdir(parents=True, exist_ok=True)

    markets = _closed_markets(max_markets=max_markets)
    if not markets:
        raise SystemExit(f"No closed live markets under {live_data_root()}")

    if split_mode == "chronological":
        markets = _assign_splits(markets, train_ratio=train_ratio, val_ratio=val_ratio)
        for split in ("train", "validation", "test"):
            (output_root / split).mkdir(parents=True, exist_ok=True)
    else:
        for m in markets:
            m["split"] = None

    total = len(markets)
    ok = 0
    skipped = 0
    failed = 0
    rows = 0
    by_split: dict[str, int] = {}
    failures: list[dict[str, Any]] = []

    print(
        f"Building live features: {total} markets -> {output_root} "
        f"(workers={workers}, split={split_mode})",
        flush=True,
    )

    def _consume(result: dict[str, Any], done: int) -> None:
        nonlocal ok, skipped, failed, rows
        mid = result.get("market_id", "")
        if result.get("ok"):
            ok += 1
            if result.get("skipped"):
                skipped += 1
            r = int(result.get("rows") or 0)
            rows += r
            split = result.get("split") or "flat"
            by_split[str(split)] = by_split.get(str(split), 0) + 1
            tag = "skip" if result.get("skipped") else "ok"
            print(f"[{done}/{total}] {tag} {mid} rows={r}", flush=True)
        else:
            failed += 1
            failures.append(result)
            print(
                f"[{done}/{total}] fail {mid} reason={result.get('reason')}",
                flush=True,
            )

    done = 0
    workers = max(1, int(workers))
    if workers <= 1:
        for market in markets:
            result = build_one_market(
                market,
                output_root=output_root,
                compression=compression,
                skip_existing=skip_existing,
                min_rows=min_rows,
            )
            done += 1
            _consume(result, done)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    build_one_market,
                    market,
                    output_root=output_root,
                    compression=compression,
                    skip_existing=skip_existing,
                    min_rows=min_rows,
                ): market
                for market in markets
            }
            for fut in as_completed(futures):
                result = fut.result()
                done += 1
                _consume(result, done)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source": str(live_data_root().resolve()),
        "output": str(output_root.resolve()),
        "compression": compression,
        "split_mode": split_mode,
        "train_ratio": train_ratio if split_mode == "chronological" else None,
        "val_ratio": val_ratio if split_mode == "chronological" else None,
        "id_columns": list(ID_COLUMNS),
        "feature_columns": list(FEATURE_COLUMNS),
        "direction_feature_columns": list(DIRECTION_FEATURE_COLUMNS),
        "output_columns": list(OUTPUT_COLUMNS),
        "markets_ok": ok,
        "markets_skipped_existing": skipped,
        "markets_failed": failed,
        "markets_total": total,
        "rows": rows,
        "by_split": by_split,
        "layout": (
            "features_live/{split}/{market_id}.parquet"
            if split_mode == "chronological"
            else "features_live/{market_id}.parquet"
        ),
        "failures": failures[:50],
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    print(
        f"Done: {ok}/{total} ok ({skipped} existing, {failed} failed), "
        f"{rows:,} rows -> {output_root}",
        flush=True,
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build prediction-model feature parquet from fetch_live markets",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output root (default: fetch_real/features_live)",
    )
    parser.add_argument("--max-markets", type=int, default=None)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--compression", type=str, default="zstd")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip markets that already have a non-empty parquet",
    )
    parser.add_argument("--min-rows", type=int, default=30)
    parser.add_argument(
        "--split",
        choices=("flat", "chronological"),
        default="flat",
        help="flat = one folder; chronological = train/validation/test by market start",
    )
    parser.add_argument("--train-ratio", type=float, default=0.8)
    parser.add_argument("--val-ratio", type=float, default=0.1)
    args = parser.parse_args(argv)

    build_live_features(
        output_root=args.output,
        max_markets=args.max_markets,
        workers=args.workers,
        compression=args.compression,
        skip_existing=args.skip_existing,
        min_rows=args.min_rows,
        split_mode=args.split,
        train_ratio=args.train_ratio,
        val_ratio=args.val_ratio,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
