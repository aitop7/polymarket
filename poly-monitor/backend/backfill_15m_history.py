"""Backfill historical 15m Up/Down markets into FETCH_LIVE_DATA_DIR.

Supports BTC 15m (`--series 15m`) and BNB 15m (`--series bnb-15m`).

Creates YYYY-MM-DD/{market_id}/ with meta.json, then fills:
  - Binance trades + 1s mid (series_repair)
  - Polymarket trades (Data API)
  - Optional PMData L2 books + Chainlink (when keys configured)

Example:
  cd poly-monitor/backend
  set PYTHONPATH=%CD%;%CD%\\..
  python backfill_15m_history.py --from-date 2026-08-07
  python backfill_15m_history.py --series bnb-15m --from-date 2026-08-07
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

# Ensure backend package imports work when run as a script.
_BACKEND = Path(__file__).resolve().parent
_POLY = _BACKEND.parent
for p in (_BACKEND, _POLY):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from app.core.config import settings  # noqa: E402
from app.core.series import MarketSeries, get_series  # noqa: E402
from app.core.series_repair import repair_binance_for_market_dir  # noqa: E402
from app.core.trade_repair import backfill_trades_for_market_dir  # noqa: E402
from app.core.pmdata_client import pmdata_enabled  # noqa: E402
from app.live.clients import parse_token_ids  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_15m")

ET = ZoneInfo("America/New_York")
GAMMA = "https://gamma-api.polymarket.com"


def _safe_name(value: str) -> str:
    import re

    text = (value or "unknown").strip()
    text = re.sub(r"[^\w.\-]+", "_", text)
    return text[:180] or "unknown"


def _utc_date_key(start_ms: int) -> str:
    return datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def _parse_outcome_prices(raw: Any) -> list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    try:
        return [float(raw[0]), float(raw[1])]
    except (TypeError, ValueError):
        return None


def parse_winner(market: dict[str, Any]) -> bool | None:
    """True=UP, False=DOWN, None=unresolved."""
    prices = _parse_outcome_prices(market.get("outcomePrices"))
    if prices is not None:
        up, down = prices
        if up >= 0.99 and down <= 0.01:
            return True
        if down >= 0.99 and up <= 0.01:
            return False
    wo = str(market.get("winningOutcome") or "").strip().lower()
    if wo in {"up", "yes"}:
        return True
    if wo in {"down", "no"}:
        return False
    return None


def iter_finished_starts(
    series: MarketSeries,
    *,
    from_date_et: str,
    until_ms: int | None = None,
) -> list[int]:
    """Unix-second starts for finished windows from ET midnight of from_date."""
    d0 = datetime.strptime(from_date_et, "%Y-%m-%d").replace(tzinfo=ET)
    t0 = int(d0.timestamp())
    t0 -= t0 % series.duration_s
    now_ms = int(until_ms if until_ms is not None else time.time() * 1000)
    out: list[int] = []
    ts = t0
    while True:
        end_ms = (ts + series.duration_s) * 1000
        if end_ms > now_ms:
            break
        out.append(ts)
        ts += series.duration_s
    return out


async def fetch_market_by_slug(
    http: httpx.AsyncClient, slug: str
) -> dict[str, Any] | None:
    try:
        resp = await http.get("/events", params={"slug": slug})
        resp.raise_for_status()
        events = resp.json()
    except Exception as exc:
        logger.warning("gamma %s failed: %s", slug, exc)
        return None
    if not isinstance(events, list) or not events:
        return None
    event = events[0]
    if not isinstance(event, dict):
        return None
    markets = event.get("markets") or []
    market: dict[str, Any] | None = None
    for row in markets:
        if not isinstance(row, dict):
            continue
        if str(row.get("slug") or "") == slug or len(markets) == 1:
            market = dict(row)
            break
    if market is None and markets and isinstance(markets[0], dict):
        market = dict(markets[0])
    if market is None:
        return None
    # Attach event strike when present.
    meta = event.get("eventMetadata")
    if isinstance(meta, dict) and meta.get("priceToBeat") is not None:
        try:
            market["priceToBeat"] = float(meta["priceToBeat"])
        except (TypeError, ValueError):
            pass
    if isinstance(meta, dict) and meta.get("finalPrice") is not None:
        try:
            market["finalPrice"] = float(meta["finalPrice"])
        except (TypeError, ValueError):
            pass
    return market


def build_meta(
    market: dict[str, Any], *, start_s: int, series: MarketSeries
) -> dict[str, Any]:
    market_id = str(market.get("id") or market.get("conditionId") or "")
    condition_id = str(market.get("conditionId") or "") or None
    token_up, token_down = parse_token_ids(market)
    start_ms = start_s * 1000
    end_ms = (start_s + series.duration_s) * 1000
    open_px = market.get("priceToBeat")
    try:
        open_f = float(open_px) if open_px is not None else None
    except (TypeError, ValueError):
        open_f = None
    close_px = market.get("finalPrice")
    try:
        close_f = float(close_px) if close_px is not None else None
    except (TypeError, ValueError):
        close_f = None
    winner = parse_winner(market)
    return {
        "market_id": market_id,
        "condition_id": condition_id,
        "slug": str(market.get("slug") or series.slug_for_start(start_s)),
        "series": series.key,
        "asset": series.asset,
        "question": str(market.get("question") or market.get("title") or ""),
        "up_token_id": token_up,
        "down_token_id": token_down,
        "start_time": start_ms,
        "end_time": end_ms,
        "resolved_at": None,
        "btc_open_price": open_f,
        "btc_close_price": close_f,
        "winner": winner,
        "active": False,
        "closed": bool(market.get("closed")) or winner is not None,
        "source": "backfill_15m_history",
    }


def market_dir_for(root: Path, meta: dict[str, Any]) -> Path:
    start_ms = int(meta["start_time"])
    mid = _safe_name(str(meta["market_id"]))
    return root / _utc_date_key(start_ms) / mid


def already_complete(market_dir: Path, *, require_pmdata: bool) -> bool:
    meta_path = market_dir / "meta.json"
    if not meta_path.is_file():
        return False
    need = [
        "binance_price_orderbook.parquet",
        "binance_trades.parquet",
        "trades.parquet",
    ]
    if require_pmdata:
        need.extend(["pm_orderbooks.parquet", "pm_chainlink_price.parquet"])
    for name in need:
        p = market_dir / name
        try:
            if not p.is_file() or p.stat().st_size <= 0:
                return False
        except OSError:
            return False
    return True


def write_meta(market_dir: Path, meta: dict[str, Any]) -> None:
    market_dir.mkdir(parents=True, exist_ok=True)
    path = market_dir / "meta.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _seed_chainlink_from_binance(market_dir: Path) -> int:
    """Fill chainlink_price.parquet from Binance_BTC 1s when Chainlink is missing."""
    import pandas as pd

    px_path = market_dir / "binance_price_orderbook.parquet"
    cl_path = market_dir / "chainlink_price.parquet"
    if not px_path.is_file():
        return 0
    try:
        if cl_path.is_file() and cl_path.stat().st_size > 0:
            existing = pd.read_parquet(cl_path)
            if not existing.empty and "Chainlink_BTC" in existing.columns:
                if int(existing["Chainlink_BTC"].notna().sum()) > 0:
                    return 0
    except Exception:
        pass
    try:
        px = pd.read_parquet(px_path)
    except Exception:
        return 0
    if px.empty or "timestamp" not in px.columns or "Binance_BTC" not in px.columns:
        return 0
    out = pd.DataFrame(
        {
            "timestamp": px["timestamp"].astype("int64"),
            "Chainlink_BTC": pd.to_numeric(px["Binance_BTC"], errors="coerce"),
            "twap": pd.to_numeric(px["Binance_BTC"], errors="coerce"),
        }
    )
    out = out.dropna(subset=["Chainlink_BTC"]).drop_duplicates("timestamp", keep="last")
    if out.empty:
        return 0
    out.to_parquet(cl_path, index=False)
    return len(out)


async def process_one(
    http: httpx.AsyncClient,
    *,
    root: Path,
    start_s: int,
    series: MarketSeries,
    sem: asyncio.Semaphore,
    do_pmdata: bool,
    force: bool,
) -> dict[str, Any]:
    slug = series.slug_for_start(start_s)
    async with sem:
        market = await fetch_market_by_slug(http, slug)
        if market is None:
            return {"slug": slug, "ok": False, "error": "not_found"}

        meta = build_meta(market, start_s=start_s, series=series)
        if not meta["market_id"]:
            return {"slug": slug, "ok": False, "error": "no_market_id"}

        mdir = market_dir_for(root, meta)
        if already_complete(mdir, require_pmdata=do_pmdata) and not force:
            return {
                "slug": slug,
                "ok": True,
                "skipped": True,
                "market_id": meta["market_id"],
                "dir": str(mdir),
            }

        # Preserve existing repair stamps when re-running.
        if (mdir / "meta.json").is_file() and not force:
            try:
                old = json.loads((mdir / "meta.json").read_text(encoding="utf-8"))
                if isinstance(old, dict):
                    for k in (
                        "trades_repaired_at",
                        "trades_repaired_complete",
                        "repair_filled",
                        "data_health",
                    ):
                        if k in old and k not in meta:
                            meta[k] = old[k]
                    # Keep series/slug authoritative from this backfill.
                    old.update(meta)
                    meta = old
            except Exception:
                pass

        write_meta(mdir, meta)
        result: dict[str, Any] = {
            "slug": slug,
            "ok": True,
            "market_id": meta["market_id"],
            "dir": str(mdir),
        }

        try:
            filled = await repair_binance_for_market_dir(mdir)
            result["binance"] = filled
            # Mirror fetch_live repair: seed chainlink_price from Binance 1s when absent.
            await asyncio.to_thread(_seed_chainlink_from_binance, mdir)
        except Exception as exc:
            result["binance_error"] = str(exc)
            logger.warning("%s binance repair failed: %s", slug, exc)

        try:
            added = await backfill_trades_for_market_dir(mdir)
            result["trades_added"] = int(added or 0)
        except Exception as exc:
            result["trades_error"] = str(exc)
            logger.warning("%s trades repair failed: %s", slug, exc)

        if do_pmdata:
            mid = str(meta["market_id"])

            def _pm_books() -> dict[str, Any]:
                from app.core.pm_orderbooks import generate_pm_orderbooks_for_market

                return generate_pm_orderbooks_for_market(mid, force_download=force)

            def _pm_chain() -> dict[str, Any]:
                from app.core.pm_chainlink import generate_pm_chainlink_for_market

                return generate_pm_chainlink_for_market(mid, force_download=force)

            try:
                result["pm_orderbooks"] = await asyncio.to_thread(_pm_books)
            except Exception as exc:
                result["pm_orderbooks_error"] = str(exc)
                logger.warning("%s pm_orderbooks failed: %s", slug, exc)
            try:
                result["pm_chainlink"] = await asyncio.to_thread(_pm_chain)
            except Exception as exc:
                result["pm_chainlink_error"] = str(exc)
                logger.warning("%s pm_chainlink failed: %s", slug, exc)

        return result


async def run(args: argparse.Namespace) -> int:
    series = get_series(getattr(args, "series", None) or "15m")
    if series.duration_s != 900:
        logger.error("backfill_15m_history only supports 15m duration series, got %s", series.key)
        return 2
    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    starts = iter_finished_starts(series, from_date_et=args.from_date)
    if args.limit and args.limit > 0:
        starts = starts[: int(args.limit)]

    do_pmdata = bool(args.pmdata) and (
        pmdata_enabled("books") or pmdata_enabled("chainlink")
    )
    if args.pmdata and not do_pmdata:
        logger.warning("PMData requested but no API key configured — skipping PMData fills")

    logger.info(
        "Backfill %s → %s from %s ET (%s finished slots) pmdata=%s concurrency=%s",
        series.key,
        root,
        args.from_date,
        len(starts),
        do_pmdata,
        args.concurrency,
    )

    sem = asyncio.Semaphore(max(1, int(args.concurrency)))
    ok = skip = fail = 0
    t0 = time.time()

    async with httpx.AsyncClient(
        base_url=GAMMA, timeout=httpx.Timeout(20.0, connect=8.0)
    ) as http:
        # Chunk to keep memory/log noise bounded.
        chunk = max(8, int(args.concurrency) * 4)
        for i in range(0, len(starts), chunk):
            batch = starts[i : i + chunk]
            tasks = [
                process_one(
                    http,
                    root=root,
                    start_s=s,
                    series=series,
                    sem=sem,
                    do_pmdata=do_pmdata,
                    force=bool(args.force),
                )
                for s in batch
            ]
            results = await asyncio.gather(*tasks)
            for r in results:
                if r.get("skipped"):
                    skip += 1
                elif r.get("ok"):
                    ok += 1
                    logger.info(
                        "ok %s mid=%s trades+%s binance=%s",
                        r.get("slug"),
                        r.get("market_id"),
                        r.get("trades_added"),
                        r.get("binance"),
                    )
                else:
                    fail += 1
                    logger.warning("fail %s %s", r.get("slug"), r.get("error"))
            done = ok + skip + fail
            elapsed = max(0.1, time.time() - t0)
            logger.info(
                "progress %s/%s ok=%s skip=%s fail=%s rate=%.2f/s",
                done,
                len(starts),
                ok,
                skip,
                fail,
                done / elapsed,
            )

    # Drop stale TWAP index so new 15m markets appear in history UI.
    try:
        from app.core.market_index import invalidate_market_index
        from app.core.live_dataset import TWAP_SPLIT

        invalidate_market_index(TWAP_SPLIT)
        logger.info("Invalidated TWAP market index cache")
    except Exception as exc:
        logger.warning("Could not invalidate market index: %s", exc)

    logger.info(
        "Done ok=%s skip=%s fail=%s elapsed=%.1fs root=%s",
        ok,
        skip,
        fail,
        time.time() - t0,
        root,
    )
    return 0 if fail == 0 else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--series",
        default="15m",
        choices=["15m", "bnb-15m"],
        help="Market series to backfill (default: BTC 15m)",
    )
    p.add_argument("--from-date", default="2026-08-07", help="ET calendar start YYYY-MM-DD")
    p.add_argument(
        "--root",
        default=str(settings.fetch_live_data_dir),
        help="Live dataset root (default FETCH_LIVE_DATA_DIR)",
    )
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--limit", type=int, default=0, help="Process only first N slots (0=all)")
    p.add_argument(
        "--pmdata",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Also fill pm_orderbooks + pm_chainlink via PMData (default: on)",
    )
    p.add_argument("--force", action="store_true", help="Re-download / overwrite existing")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
