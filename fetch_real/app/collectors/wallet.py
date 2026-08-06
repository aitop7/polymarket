from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from app.storage.market_sessions import sessions
from app.utils.logger import logger
from app.utils.time import utcnow


class WalletCollector:
    """
    Periodic wallet snapshots.

    Note: history + live trades already append running wallet_positions on each fill.
    This collector can push an extra heartbeat snapshot of known wallets.
    """

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
        shares = float(trade.get("shares") if trade.get("shares") is not None else trade.get("size") or 0)
        is_buy = trade.get("side")
        if isinstance(is_buy, str):
            is_buy = str(is_buy).lower() in {"buy", "b", "1", "true"}
        else:
            is_buy = bool(is_buy)
        is_up = trade.get("token")
        if is_up is None:
            oc = str(trade.get("outcome") or "").lower()
            is_up = oc in {"yes", "up"} if oc else True
        else:
            is_up = bool(is_up)
        delta = shares if is_buy else -shares
        if is_up:
            self._positions[key]["yes_position"] += delta
        else:
            self._positions[key]["no_position"] += delta

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
                    "wallet": wallet,
                    "yes_position": vals["yes_position"],
                    "no_position": vals["no_position"],
                    "pnl": vals["pnl"],
                },
            )
            n += 1
        return n
