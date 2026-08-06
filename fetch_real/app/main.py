"""
Polymarket + BTC data collector entrypoint.

Modes:
  history  - backfill historical data as compressed parquet
  realtime - continuous collectors writing parquet
  both     - history sync once, then realtime
"""

from __future__ import annotations

import argparse
import asyncio
import signal

from app.config import settings
from app.scheduler.tasks import CollectorScheduler
from app.services.synchronizer import HistorySynchronizer
from app.utils.logger import logger, setup_logger


async def run_history(lookback_days: int | None) -> None:
    sync = HistorySynchronizer(lookback_days=lookback_days)
    try:
        results = await sync.sync_all()
        logger.info("History fetch done: {}", results)
    finally:
        await sync.close()


async def run_realtime() -> None:
    scheduler = CollectorScheduler()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        logger.info("Shutdown requested")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _request_stop())

    await scheduler.start()
    try:
        await stop_event.wait()
    finally:
        await scheduler.stop()


async def async_main(args: argparse.Namespace) -> None:
    setup_logger()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Parquet store ready dir={} compression={}",
        settings.data_dir.resolve(),
        settings.parquet_compression,
    )

    if args.mode in {"history", "both"}:
        await run_history(args.lookback_days)

    if args.mode in {"realtime", "both"}:
        await run_realtime()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket / BTC parquet data fetcher")
    parser.add_argument(
        "mode",
        choices=["history", "realtime", "both"],
        help="history=backfill parquet, realtime=live collectors, both=history then live",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="History lookback window in days (default from .env)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
