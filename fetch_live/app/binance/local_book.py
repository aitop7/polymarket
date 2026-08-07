"""Local Binance order book from depth diffs."""

from __future__ import annotations

from typing import Any


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

    def best_bid_ask(self) -> tuple[float | None, float | None]:
        best_bid = max(self.bids.keys()) if self.bids else None
        best_ask = min(self.asks.keys()) if self.asks else None
        return best_bid, best_ask

    def mid_price(self) -> float | None:
        bid, ask = self.best_bid_ask()
        if bid is not None and ask is not None:
            return (float(bid) + float(ask)) / 2.0
        if bid is not None:
            return float(bid)
        if ask is not None:
            return float(ask)
        return None
