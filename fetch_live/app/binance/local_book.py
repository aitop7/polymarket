"""Local Binance order book from depth diffs."""

from __future__ import annotations

from typing import Any

from app.schemas import BTC_DEPTH_COLUMNS


class LocalOrderBook:
    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_update_id: int | None = None
        self.ready = False

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.last_update_id = None
        self.ready = False

    def apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.bids.clear()
        self.asks.clear()
        for price_s, qty_s in snapshot.get("bids") or []:
            p, q = float(price_s), float(qty_s)
            if q > 0:
                self.bids[p] = q
        for price_s, qty_s in snapshot.get("asks") or []:
            p, q = float(price_s), float(qty_s)
            if q > 0:
                self.asks[p] = q
        self.last_update_id = int(snapshot.get("lastUpdateId") or 0)
        self.ready = True

    def apply_diff(self, event: dict[str, Any]) -> bool:
        """Apply depthUpdate. Returns False if resync needed."""
        if not self.ready:
            return False
        first = int(event.get("U") or 0)
        final = int(event.get("u") or 0)
        if self.last_update_id is not None and final <= self.last_update_id:
            return True
        if self.last_update_id is not None and first > self.last_update_id + 1:
            return False
        for price_s, qty_s in event.get("b") or []:
            p, q = float(price_s), float(qty_s)
            if q == 0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = q
        for price_s, qty_s in event.get("a") or []:
            p, q = float(price_s), float(qty_s)
            if q == 0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = q
        self.last_update_id = final
        return True

    def top_levels(self, n: int = 10) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:n]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:n]
        return bids, asks

    def depth_row(self, timestamp_ms: int, n: int = 10) -> dict[str, Any]:
        bids, asks = self.top_levels(n)
        row: dict[str, Any] = {"timestamp": int(timestamp_ms)}
        for i in range(1, n + 1):
            if i <= len(bids):
                row[f"bid_price_{i}"] = float(bids[i - 1][0])
                row[f"bid_qty_{i}"] = float(bids[i - 1][1])
            else:
                row[f"bid_price_{i}"] = None
                row[f"bid_qty_{i}"] = None
            if i <= len(asks):
                row[f"ask_price_{i}"] = float(asks[i - 1][0])
                row[f"ask_qty_{i}"] = float(asks[i - 1][1])
            else:
                row[f"ask_price_{i}"] = None
                row[f"ask_qty_{i}"] = None
        return {c: row.get(c) for c in BTC_DEPTH_COLUMNS}
