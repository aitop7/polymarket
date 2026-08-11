"""Polymarket Data API trades — maker + taker fills for trades.parquet."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx
from loguru import logger

from app.config import settings

OnTradeRows = Callable[[list[dict[str, Any]]], None]


def _taker_key(tx: str, wallet: str) -> str:
    return f"{tx.strip().lower()}|{wallet.strip().lower()}"


def shares_2(value: Any) -> float:
    """Non-negative share amount with 2 decimal places."""
    try:
        return round(max(0.0, float(value or 0)), 2)
    except (TypeError, ValueError):
        return 0.0


def taker_wallets_by_tx(taker_raw: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Map transaction_hash → wallets seen in takerOnly=true rows."""
    out: dict[str, set[str]] = {}
    for trade in taker_raw:
        tx = str(trade.get("transactionHash") or trade.get("transaction_hash") or "").strip()
        wallet = str(
            trade.get("proxyWallet")
            or trade.get("proxy_wallet")
            or trade.get("wallet")
            or ""
        ).strip()
        if not tx or not wallet:
            continue
        out.setdefault(tx.lower(), set()).add(wallet.lower())
    return out


def classify_is_taker(
    tx: str, wallet: str, takers_by_tx: dict[str, set[str]]
) -> bool:
    """
    True if this wallet is a known taker for the tx.

    Only mark maker when *this tx* has at least one known taker.
    A non-empty global set from other txs must not force is_taker=false.
    """
    tx_l = (tx or "").strip().lower()
    w_l = (wallet or "").strip().lower()
    if not tx_l or not w_l:
        return True
    known = takers_by_tx.get(tx_l)
    if not known:
        return True
    return w_l in known


def _raw_fill_key(trade: dict[str, Any]) -> str:
    """Identity for a single API fill row (may repeat for equal size/price levels)."""
    tx = str(trade.get("transactionHash") or trade.get("transaction_hash") or "")
    wallet = str(
        trade.get("proxyWallet")
        or trade.get("proxy_wallet")
        or trade.get("wallet")
        or ""
    )
    asset = str(trade.get("asset") or trade.get("asset_id") or "")
    side = str(trade.get("side") or "").upper()
    try:
        size = f"{round(float(trade.get('size') or 0), 2):.2f}"
    except (TypeError, ValueError):
        size = str(trade.get("size") or "")
    try:
        price = f"{round(float(trade.get('price') or 0), 6):.6f}"
    except (TypeError, ValueError):
        price = str(trade.get("price") or "")
    outcome = str(trade.get("outcome") or trade.get("outcomeIndex") or "").strip().lower()
    return f"{tx.lower()}|{wallet.lower()}|{asset}|{side}|{size}|{price}|{outcome}"


def _merge_trade_raw(
    all_raw: list[dict[str, Any]], taker_raw: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    Prefer takerOnly=false as the fill source of truth.

    Only append a takerOnly=true row when that exact fill is missing from all_raw
    (rare API gaps). Matching uses rounded price/size so float noise cannot
    double-count overlapping feeds.
    """
    from collections import Counter

    merged: list[dict[str, Any]] = list(all_raw)
    covered = Counter(_raw_fill_key(t) for t in all_raw)
    for trade in taker_raw:
        key = _raw_fill_key(trade)
        if covered[key] > 0:
            covered[key] -= 1
            continue
        merged.append(trade)
    return merged


def trim_page_overlap(
    prev_keys: list[str], batch: list[dict[str, Any]], batch_keys: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Drop leading rows of a page that repeat the tail of the previous pages.

    The Data API is newest-first and offset-paged. On a live market, trades
    arriving between two page requests push the list down, so ``offset=500``
    re-returns rows already read at the end of ``offset=0``. That repeat is
    always a contiguous prefix/suffix, so trimming the longest match removes
    the paging artifact without touching genuine identical fills.
    """
    max_k = min(len(prev_keys), len(batch_keys))
    for k in range(max_k, 0, -1):
        if prev_keys[-k:] == batch_keys[:k]:
            return batch[k:], batch_keys[k:]
    return batch, batch_keys


def _fill_base_key(row: dict[str, Any]) -> str:
    """Logical fill identity (without occurrence index)."""
    tx = str(row.get("transaction_hash") or "").strip().lower()
    wallet = str(row.get("wallet") or "").strip().lower()
    is_up = int(bool(row.get("is_up")))
    is_buy = int(bool(row.get("is_buy")))
    try:
        price = f"{round(float(row.get('price') or 0), 6):.6f}"
    except (TypeError, ValueError):
        price = "0.000000"
    try:
        shares = f"{shares_2(row.get('shares')):.2f}"
    except (TypeError, ValueError):
        shares = "0.00"
    if tx:
        return f"{tx}|{wallet}|{is_up}|{is_buy}|{price}|{shares}"
    return (
        f"notx:{int(row.get('timestamp') or 0)}|{wallet}|{is_up}|{is_buy}|{price}|{shares}"
    )


def drop_uniform_tx_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Undo whole-transaction duplication left by a feed that replayed rows.

    A page overlap repeats a contiguous block, so an affected tx comes back with
    every one of its distinct legs multiplied by the same factor. Dividing by the
    gcd of the leg counts restores the real fills. Genuine repeated legs never
    share a factor across the whole tx (the taker leg is unique), so real Orbscan
    duplicates are left untouched.
    """
    from collections import Counter, defaultdict
    from math import gcd

    if not rows:
        return []
    by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    out: list[dict[str, Any]] = []
    for row in rows:
        tx = str(row.get("transaction_hash") or "").strip().lower()
        if not tx:
            out.append(row)
            continue
        by_tx[tx].append(row)

    for group in by_tx.values():
        counts = Counter(_fill_base_key(r) for r in group)
        factor = 0
        for n in counts.values():
            factor = gcd(factor, n)
        if len(counts) < 2 or factor < 2:
            out.extend(group)
            continue
        kept: Counter[str] = Counter()
        for r in group:
            key = _fill_base_key(r)
            if kept[key] >= counts[key] // factor:
                continue
            kept[key] += 1
            out.append(r)
    return out


def assign_fill_keys(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Keep every fill as its own row (Orbscan: same wallet may appear many times).
    Attach stable fill_key + fill_index (0,1,...) for identical legs.
    """
    from collections import Counter

    if not rows:
        return []
    prepared: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        r["shares"] = shares_2(r.get("shares"))
        try:
            r["price"] = round(float(r.get("price") or 0), 6)
        except (TypeError, ValueError):
            r["price"] = 0.0
        prepared.append(r)
    # Stable order so re-sync keeps occurrence indices when possible.
    prepared.sort(
        key=lambda r: (
            _fill_base_key(r),
            int(r.get("fill_index") or 0),
            str(r.get("fill_key") or ""),
        )
    )
    counts: Counter[str] = Counter()
    out: list[dict[str, Any]] = []
    for r in prepared:
        base = _fill_base_key(r)
        occ = counts[base]
        counts[base] += 1
        r["fill_index"] = int(occ)
        r["fill_key"] = f"{base}|{occ}"
        out.append(r)
    return out


def aggregate_wallet_shares(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Backward-compatible name: do not merge wallets; assign fill keys only."""
    return assign_fill_keys(rows)


def normalize_trade_legs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Orbscan-style legs per transaction_hash:
      - keep every fill row (same wallet may have several actions/prices)
      - keep each row's own is_up (outcome), is_buy (action), and price
      - exactly one primary taker *wallet*; other wallets demoted to maker
    """
    if not rows:
        return []
    by_tx: dict[str, list[dict[str, Any]]] = {}
    orphan: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        r["wallet"] = str(r.get("wallet") or "").strip().lower()
        tx = str(r.get("transaction_hash") or "").strip().lower()
        if not tx:
            orphan.append(r)
            continue
        by_tx.setdefault(tx, []).append(r)

    out: list[dict[str, Any]] = list(orphan)
    for group in by_tx.values():
        takers = [r for r in group if r.get("is_taker")]
        if not takers:
            if len({str(r.get("wallet") or "").lower() for r in group}) <= 1:
                out.extend(group)
                continue
            takers = list(group)
        taker = min(
            takers,
            key=lambda r: (
                -float(r.get("shares") or 0),
                0 if r.get("is_buy") else 1,
                int(r.get("timestamp") or 0),
                str(r.get("wallet") or "").lower(),
            ),
        )
        taker_wallet = str(taker.get("wallet") or "").strip().lower()
        for r in group:
            r["is_taker"] = str(r.get("wallet") or "").strip().lower() == taker_wallet
            out.append(r)
    return assign_fill_keys(out)


class DataApiTrades:
    def __init__(self, *, on_trades: OnTradeRows | None = None) -> None:
        self.on_trades = on_trades
        self._http = httpx.AsyncClient(
            base_url=settings.data_api_url,
            timeout=httpx.Timeout(15.0, connect=8.0),
        )
        self._running = False
        self._condition_id: str | None = None
        self._token_up: str | None = None
        self._token_down: str | None = None
        self._start_ms: int | None = None
        self._end_ms: int | None = None

    def set_market(
        self,
        *,
        condition_id: str | None,
        token_up: str | None,
        token_down: str | None,
        start_ms: int,
        end_ms: int,
    ) -> None:
        self._condition_id = condition_id or None
        self._token_up = token_up
        self._token_down = token_down
        self._start_ms = int(start_ms)
        self._end_ms = int(end_ms)

    async def close(self) -> None:
        self._running = False
        await self._http.aclose()

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.poll_once()
            except Exception as exc:
                logger.warning("Data API trades poll failed: {}", exc)
            await asyncio.sleep(settings.trades_poll_interval_s)

    async def poll_once(self) -> None:
        if not self._condition_id or not self.on_trades:
            return
        rows = await self.fetch_window(
            condition_id=self._condition_id,
            token_up=self._token_up,
            token_down=self._token_down,
            start_ms=self._start_ms or 0,
            end_ms=self._end_ms or 0,
            max_pages=5,
        )
        if rows:
            self.on_trades(rows)

    async def _fetch_pages(
        self,
        *,
        condition_id: str,
        max_pages: int,
        start_ms: int,
        taker_only: bool,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        out_keys: list[str] = []
        offset = 0
        for _ in range(max_pages):
            batch: list[Any] | None = None
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    resp = await self._http.get(
                        "/trades",
                        params={
                            "market": condition_id,
                            "limit": 500,
                            "offset": offset,
                            "takerOnly": str(taker_only).lower(),
                        },
                    )
                    resp.raise_for_status()
                    raw = resp.json()
                    batch = raw if isinstance(raw, list) else []
                    break
                except Exception as exc:
                    last_exc = exc
                    await asyncio.sleep(0.4 * (attempt + 1))
            if batch is None:
                raise last_exc or RuntimeError("Data API fetch failed")
            if not batch:
                break
            page = [item for item in batch if isinstance(item, dict)]
            page, page_keys = trim_page_overlap(
                out_keys, page, [_raw_fill_key(t) for t in page]
            )
            out.extend(page)
            out_keys.extend(page_keys)
            oldest = min(int(t.get("timestamp") or 0) for t in batch)
            if oldest and oldest < 10_000_000_000:
                oldest *= 1000
            too_old = bool(start_ms and oldest < start_ms - 300_000)
            if len(batch) < 500 or too_old:
                break
            offset += 500
        return out

    async def fetch_window(
        self,
        *,
        condition_id: str,
        token_up: str | None,
        token_down: str | None,
        start_ms: int,
        end_ms: int,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Fetch all fills (makers + takers) for a market window.

        Classification: takerOnly=true builds per-tx taker wallets; other fills
        on a tx that has a known taker are makers. If a tx has no known taker,
        do not invent makers (default is_taker=true).
        """
        if not condition_id:
            return []

        all_raw, taker_raw = await asyncio.gather(
            self._fetch_pages(
                condition_id=condition_id,
                max_pages=max_pages,
                start_ms=start_ms,
                taker_only=False,
            ),
            self._fetch_pages(
                condition_id=condition_id,
                max_pages=max_pages,
                start_ms=start_ms,
                taker_only=True,
            ),
        )

        takers_by_tx = taker_wallets_by_tx(taker_raw)
        out: list[dict[str, Any]] = []
        # Emit fills from takerOnly=false only. takerOnly=true is classification
        # only — merging both feeds double-counted when float/format keys drifted.
        for trade in all_raw:
            row = self._to_row(
                trade,
                token_up=token_up,
                token_down=token_down,
                start_ms=start_ms,
                end_ms=end_ms,
                takers_by_tx=takers_by_tx,
            )
            if row is None:
                continue
            out.append(row)
        # Classify + assign fill_index (do not wallet-merge).
        return normalize_trade_legs(drop_uniform_tx_duplicates(out))

    def _to_row(
        self,
        trade: dict[str, Any],
        *,
        token_up: str | None,
        token_down: str | None,
        start_ms: int,
        end_ms: int,
        takers_by_tx: dict[str, set[str]],
    ) -> dict[str, Any] | None:
        tx = str(trade.get("transactionHash") or trade.get("transaction_hash") or "")
        try:
            ts = int(trade.get("timestamp") or 0)
        except (TypeError, ValueError):
            return None
        if ts < 10_000_000_000:
            ts *= 1000
        # Allow pre-open prints (same conditionId); reject post-end and absurdly early.
        if end_ms and ts >= end_ms:
            return None
        if start_ms and ts < start_ms - 300_000:
            return None

        asset = str(trade.get("asset") or trade.get("asset_id") or "")
        is_up: bool | None = None
        if token_up and asset == str(token_up):
            is_up = True
        elif token_down and asset == str(token_down):
            is_up = False
        else:
            outcome = str(trade.get("outcome") or "").strip().lower()
            if outcome in {"up", "yes"}:
                is_up = True
            elif outcome in {"down", "no"}:
                is_up = False
            else:
                try:
                    # outcomeIndex 0=Up, 1=Down on BTC up/down markets
                    is_up = int(trade.get("outcomeIndex")) == 0
                except (TypeError, ValueError):
                    return None
        if is_up is None:
            return None

        side_raw = str(trade.get("side") or "BUY").upper()
        is_buy = side_raw not in {"SELL", "S"}
        try:
            price = float(trade.get("price") or 0)
            size = float(trade.get("size") or 0)
        except (TypeError, ValueError):
            return None

        wallet = str(
            trade.get("proxyWallet")
            or trade.get("proxy_wallet")
            or trade.get("wallet")
            or ""
        ).strip().lower()
        is_taker = classify_is_taker(tx, wallet, takers_by_tx)

        return {
            "timestamp": ts,
            "transaction_hash": tx,
            "wallet": wallet,
            "is_up": bool(is_up),
            "is_buy": bool(is_buy),
            "is_taker": bool(is_taker),
            "price": price,
            "shares": shares_2(size),
        }
