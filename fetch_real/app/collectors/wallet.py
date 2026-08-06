from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from app.storage.market_sessions import sessions
from app.utils.logger import logger
from app.utils.time import utcnow


class WalletCollector:
    """Optional wallet snapshots appended into market session files when market_id is known."""

    def __init__(self, interval_s: float = 60.0) -> None:
        self.interval_s = interval_s
        self._running = False
        self._positions: dict[tuple[str, str], dict[str, float]] = defaultdict(
            lambda: {"yes_position": 0.0, "no_position": 0.0, "pnl": 0.0}
        )

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.snapshot_once()
            except Exception as exc:
                logger.exception("Wallet collector error: {}", exc)
            await asyncio.sleep(self.interval_s)

    def stop(self) -> None:
        self._running = False

    def apply_trade(self, trade: dict[str, Any]) -> None:
        wallet = trade.get("wallet")
        market_id = trade.get("market_id")
        if not wallet or not market_id:
            return
        key = (str(wallet), str(market_id))
        size = float(trade.get("size") or 0)
        side = str(trade.get("side") or "").lower()
        if side in {"buy", "b"}:
            self._positions[key]["yes_position"] += size
        else:
            self._positions[key]["no_position"] += size

    async def snapshot_once(self) -> int:
        if not self._positions:
            return 0
        ts = utcnow()
        n = 0
        for (wallet, market_id), vals in self._positions.items():
            sessions.append(
                market_id,
                "wallet",
                {
                    "timestamp": ts,
                    "market_id": market_id,
                    "wallet": wallet,
                    "yes_position": vals["yes_position"],
                    "no_position": vals["no_position"],
                    "pnl": vals["pnl"],
                },
            )
            n += 1
        return n
