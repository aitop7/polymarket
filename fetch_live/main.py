"""Entry point: live Polymarket + Binance collector."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

# Allow `python main.py` from fetch_live/
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger

from app.orchestrator import Orchestrator


async def _amain() -> None:
    orch = Orchestrator()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _signal(*_args: object) -> None:
        if not stop.is_set():
            logger.info("Signal received — shutting down")
            stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal)
        except (NotImplementedError, RuntimeError):
            pass

    task = asyncio.create_task(orch.run())
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        # Fallback for Windows where SIGINT may not use add_signal_handler
        pass


if __name__ == "__main__":
    main()
