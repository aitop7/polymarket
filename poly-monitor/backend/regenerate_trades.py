"""Rewrite every trades.parquet under FETCH_LIVE_DATA_DIR with the Orbscan schema.

New columns: timestamp, transaction_hash, wallet, is_up, is_buy, is_taker,
price, shares, fill_index.

Usage:
  cd poly-monitor/backend
  python regenerate_trades.py
  python regenerate_trades.py --root "E:\\DataSets\\poly\\live" --concurrency 6
  python regenerate_trades.py --only-old   # skip files that already have fill_index
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

import pyarrow.parquet as pq

from app.core.trade_repair import backfill_trades_for_market_dir

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("regenerate_trades")


def _has_new_schema(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        cols = set(pq.read_schema(path).names)
    except Exception:
        return False
    return {"transaction_hash", "is_up", "is_buy", "is_taker", "fill_index"}.issubset(
        cols
    )


def _market_dirs(root: Path) -> list[Path]:
    out: list[Path] = []
    for day in sorted(root.iterdir()):
        if not day.is_dir():
            continue
        for market in sorted(day.iterdir()):
            if market.is_dir() and (market / "meta.json").is_file():
                out.append(market)
    return out


async def _one(
    market_dir: Path,
    *,
    sem: asyncio.Semaphore,
    only_old: bool,
    stats: dict[str, int],
) -> None:
    trades = market_dir / "trades.parquet"
    mid = f"{market_dir.parent.name}/{market_dir.name}"
    if only_old and _has_new_schema(trades):
        stats["skipped"] += 1
        return
    async with sem:
        t0 = time.perf_counter()
        n = 0
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                n = await backfill_trades_for_market_dir(market_dir)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(1.5 * attempt)
        if last_exc is not None:
            stats["failed"] += 1
            logger.warning("FAIL %s: %s", mid, last_exc)
            return
        dt = time.perf_counter() - t0
        if n <= 0:
            stats["empty"] += 1
            logger.warning("EMPTY %s (kept existing file) %.1fs", mid, dt)
            return
        stats["ok"] += 1
        stats["rows"] += n
        done = stats["ok"] + stats["failed"] + stats["empty"]
        total = stats["total"]
        if done % 25 == 0 or done <= 5:
            logger.info(
                "OK %s → %d rows (%.1fs) [%d/%d]",
                mid,
                n,
                dt,
                done,
                total,
            )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        type=Path,
        default=Path(r"E:\DataSets\poly\live"),
        help="fetch_live data root",
    )
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument(
        "--only-old",
        action="store_true",
        help="Skip markets whose trades.parquet already has the new schema",
    )
    args = ap.parse_args()
    root: Path = args.root
    if not root.is_dir():
        logger.error("Root not found: %s", root)
        return 1

    markets = _market_dirs(root)
    if args.only_old:
        markets = [
            m for m in markets if not _has_new_schema(m / "trades.parquet")
        ]
    stats = {
        "total": len(markets),
        "ok": 0,
        "failed": 0,
        "empty": 0,
        "skipped": 0,
        "rows": 0,
    }
    logger.info(
        "Regenerating %d markets under %s (concurrency=%d)",
        len(markets),
        root,
        args.concurrency,
    )
    sem = asyncio.Semaphore(max(1, int(args.concurrency)))
    await asyncio.gather(
        *[_one(m, sem=sem, only_old=False, stats=stats) for m in markets]
    )
    logger.info(
        "Done: ok=%d empty=%d failed=%d skipped=%d rows=%d",
        stats["ok"],
        stats["empty"],
        stats["failed"],
        stats["skipped"],
        stats["rows"],
    )
    return 0 if stats["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
