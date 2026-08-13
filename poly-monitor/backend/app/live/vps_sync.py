"""Pull fetch_live market dirs from VPS serve API into local FETCH_LIVE_DATA_DIR."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings
from app.core.live_dataset import (
    PRICE_SERIES_STEP_MS as _PRICE_STEP_MS,
    TRADE_NOTE_MS as _TRADE_NOTE_MS,
    TRADE_REPAIR_MS as _TRADE_GRADE_MS,
)

_ET = ZoneInfo("America/New_York")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_GAP_NOTES = 12

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache"
_WATERMARK_PATH = _CACHE_DIR / "fetch_live_sync.json"
_ACTIVE_REFRESH_S = 12.0
PERIODIC_SYNC_S = 60.0
_REPAIR_LOOKBACK_MS = 2 * 60 * 60 * 1000  # re-check last 2h for gaps
_EXPECTED_FILES = (
    "meta.json",
    "binance_trades.parquet",
    "binance_price_orderbook.parquet",
    "chainlink_price.parquet",
    "orderbooks.parquet",
    "trades.parquet",
)
# 1 Hz price / book series — any skipped second is a miss.
_PRICE_1S_FILES = (
    "binance_price_orderbook.parquet",
    "chainlink_price.parquet",
    "orderbooks.parquet",
)
# Trade tapes graded with TRADE_* thresholds from live_dataset.
_TRADE_FILES = (
    "binance_trades.parquet",
    "trades.parquet",
)

# Back-compat names.
_TRADE_GAP_MS = _TRADE_GRADE_MS
_PRICE_GAP_MS = _PRICE_STEP_MS


class VpsSyncClient:
    def __init__(self) -> None:
        self._last_active_pull: dict[str, float] = {}
        self._last_error_log = 0.0
        self._unreachable = False
        self._hist_locks: dict[str, asyncio.Lock] = {}

    def _hist_lock(self, market_id: str) -> asyncio.Lock:
        lock = self._hist_locks.get(market_id)
        if lock is None:
            lock = asyncio.Lock()
            self._hist_locks[market_id] = lock
        return lock

    @property
    def enabled(self) -> bool:
        return settings.vps_sync_enabled

    @property
    def data_dir(self) -> Path:
        return Path(settings.fetch_live_data_dir)

    def _headers(self) -> dict[str, str]:
        token = (settings.vps_sync_token or "").strip()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    def _base(self) -> str:
        return settings.vps_sync_base_url

    def _log_net_error(self, where: str, exc: BaseException) -> None:
        now = time.monotonic()
        # Avoid traceback spam when VPS serve is down / firewall resets.
        if now - self._last_error_log < 60.0:
            return
        self._last_error_log = now
        self._unreachable = True
        logger.warning(
            "VPS sync %s failed (%s): %s — is fetch_live serve running at %s ?",
            where,
            type(exc).__name__,
            exc,
            self._base() or "(unset)",
        )

    def _http(self, *, read_s: float = 20.0) -> httpx.AsyncClient:
        # Connect short; read longer for archive zips on recheck/pull.
        return httpx.AsyncClient(
            timeout=httpx.Timeout(max(30.0, read_s + 10.0), connect=5.0, read=read_s),
            headers=self._headers(),
        )

    def load_watermark(self) -> dict[str, Any]:
        if not _WATERMARK_PATH.is_file():
            return {"after_start_ms": 0, "updated_at": None}
        try:
            raw = json.loads(_WATERMARK_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"after_start_ms": 0, "updated_at": None}
        if not isinstance(raw, dict):
            return {"after_start_ms": 0, "updated_at": None}
        try:
            after = int(raw.get("after_start_ms") or 0)
        except (TypeError, ValueError):
            after = 0
        return {
            "after_start_ms": after,
            "updated_at": raw.get("updated_at"),
            "synced_market_ids": raw.get("synced_market_ids") or [],
        }

    def save_watermark(self, after_start_ms: int, *, synced_ids: list[str] | None = None) -> None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "after_start_ms": int(after_start_ms),
            "updated_at": int(time.time() * 1000),
            "synced_market_ids": list(synced_ids or [])[-500:],
        }
        tmp = _WATERMARK_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(_WATERMARK_PATH)

    async def _get_json(self, client: httpx.AsyncClient, path: str, **params: Any) -> Any:
        url = f"{self._base()}{path}"
        resp = await client.get(url, params=params or None)
        resp.raise_for_status()
        return resp.json()

    async def list_markets(
        self, client: httpx.AsyncClient, *, after_start_ms: int = 0
    ) -> list[dict[str, Any]]:
        data = await self._get_json(
            client, "/markets", after_start_ms=int(after_start_ms)
        )
        markets = data.get("markets") if isinstance(data, dict) else None
        if not isinstance(markets, list):
            return []
        return [m for m in markets if isinstance(m, dict)]

    async def get_market(
        self, client: httpx.AsyncClient, market_id: str
    ) -> dict[str, Any] | None:
        try:
            data = await self._get_json(client, f"/markets/{market_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return data if isinstance(data, dict) else None

    def _local_dir(self, date: str, market_id: str) -> Path:
        return self.data_dir / str(date) / str(market_id)

    def _local_incomplete(self, dest: Path, remote: dict[str, Any] | None = None) -> bool:
        if not dest.is_dir() or not (dest / "meta.json").is_file():
            return True
        remote_files = {
            str(f.get("name")): int(f.get("size") or 0)
            for f in (remote or {}).get("files") or []
            if isinstance(f, dict) and f.get("name")
        }
        if not remote_files:
            # Require price/orderbook tables when we have no remote listing.
            # pm_orderbooks.parquet satisfies the orderbooks requirement.
            from app.core.live_dataset import ORDERBOOKS_FILE, resolve_orderbooks_path

            for name in ("meta.json", *_PRICE_1S_FILES):
                if name == ORDERBOOKS_FILE:
                    if resolve_orderbooks_path(dest) is None:
                        return True
                    continue
                if not (dest / name).is_file():
                    return True
            return False
        from app.core.live_dataset import ORDERBOOKS_FILE, PM_ORDERBOOKS_FILE, resolve_orderbooks_path

        for name, size in remote_files.items():
            # Local pm_orderbooks covers the live orderbooks.parquet requirement.
            if name in {ORDERBOOKS_FILE, PM_ORDERBOOKS_FILE}:
                if resolve_orderbooks_path(dest) is not None:
                    continue
            p = dest / name
            if not p.is_file():
                return True
            try:
                if int(p.stat().st_size) < size:
                    return True
            except OSError:
                return True
        return False

    def _parquet_timestamps(self, path: Path) -> list[int]:
        if not path.is_file():
            return []
        try:
            import pyarrow.parquet as pq

            table = pq.read_table(path, columns=["timestamp"])
            out: list[int] = []
            for v in table.column("timestamp").to_pylist():
                if v is None:
                    continue
                try:
                    out.append(int(v))
                except (TypeError, ValueError):
                    continue
            return out
        except Exception:
            return []

    def _remote_names(self, remote: dict[str, Any]) -> set[str]:
        return {
            str(f.get("name"))
            for f in (remote.get("files") or [])
            if isinstance(f, dict) and f.get("name")
        }

    @staticmethod
    def _fmt_et(ms: int) -> str:
        try:
            return datetime.fromtimestamp(ms / 1000.0, tz=_ET).strftime("%H:%M:%S")
        except (OSError, ValueError, OverflowError):
            return str(ms)

    @staticmethod
    def _fmt_gap_detail(a: int, b: int, *, kind: str) -> str:
        dur_s = max(0, int(round((b - a) / 1000.0)))
        return (
            f"{kind} {VpsSyncClient._fmt_et(a)}-{VpsSyncClient._fmt_et(b)} ET "
            f"({dur_s}s)"
        )

    @staticmethod
    def _fmt_gap_note(name: str, a: int, b: int, *, kind: str) -> str:
        return f"{name}: {VpsSyncClient._fmt_gap_detail(a, b, kind=kind)}"

    @staticmethod
    def _format_notes_by_file(
        by_file: dict[str, list[tuple[int, str]]],
        *,
        per_file: int = 4,
    ) -> tuple[dict[str, list[str]], list[str], str]:
        """
        Group scored gaps by file (worst files / gaps first).
        Returns (notes_by_file, flat display lines, comment string).
        """
        ranked: list[tuple[int, str, list[tuple[int, str]]]] = []
        for name, items in by_file.items():
            if not items:
                continue
            items_sorted = sorted(items, key=lambda x: x[0], reverse=True)
            ranked.append((items_sorted[0][0], name, items_sorted))
        ranked.sort(key=lambda x: x[0], reverse=True)

        notes_by_file: dict[str, list[str]] = {}
        lines: list[str] = []
        for _, name, items in ranked:
            details = [text for _, text in items[:per_file]]
            notes_by_file[name] = details
            lines.append(name)
            for detail in details:
                lines.append(f"  {detail}")
        return notes_by_file, lines, "\n".join(lines)

    def _price_series_stats(
        self,
        path: Path,
        *,
        name: str,
        start: int,
        end: int,
        remote_has: bool | None,
        step_ms: int | None = None,
    ) -> tuple[int, list[tuple[int, str]]]:
        """Return (max_gap_ms, scored notes) for a price/orderbook series."""
        step = max(1, int(step_ms if step_ms is not None else _PRICE_STEP_MS))
        if not path.is_file():
            if remote_has is False:
                return 0, []
            span = max(end - start, step + 1)
            return span, [(span, "missing file")]
        ts = sorted(t for t in set(self._parquet_timestamps(path)) if start <= t <= end)
        if not ts:
            span = max(end - start, step + 1)
            return span, [(span, "empty file")]
        max_gap = 0
        scored: list[tuple[int, str]] = []

        def consider(a: int, b: int, *, kind: str) -> None:
            nonlocal max_gap
            gap = b - a
            if gap > step:
                max_gap = max(max_gap, gap)
                scored.append((gap, self._fmt_gap_detail(a, b, kind=kind)))

        consider(start, ts[0], kind="missing")
        consider(ts[-1], end, kind="missing")
        for a, b in zip(ts, ts[1:]):
            consider(a, b, kind="gap")
        return max_gap, scored

    def _trade_series_stats(
        self,
        path: Path,
        *,
        name: str,
        start: int,
        end: int,
        remote_has: bool | None,
    ) -> tuple[int, list[tuple[int, str]]]:
        """
        Return (worst single quiet_ms, scored notes) for a trade tape.
        Only quiets inside [start, end] count — never a sum.
        """
        if not path.is_file():
            if remote_has is False:
                return 0, []
            span = max(end - start, _TRADE_GRADE_MS + 1)
            return span, [(span, "missing file")]
        ts = sorted(t for t in set(self._parquet_timestamps(path)) if start <= t <= end)
        if not ts:
            span = end - start
            if span > 0:
                return span, [(span, "empty file (no trades in window)")]
            return 0, []
        max_quiet = 0
        scored: list[tuple[int, str]] = []

        def consider(a: int, b: int) -> None:
            nonlocal max_quiet
            quiet = b - a
            if quiet > _PRICE_STEP_MS:
                max_quiet = max(max_quiet, quiet)
            # Note anything that leaves trade Great (<2s).
            if quiet >= _TRADE_NOTE_MS:
                scored.append((quiet, self._fmt_gap_detail(a, b, kind="quiet")))

        consider(start, ts[0])
        consider(ts[-1], end)
        for a, b in zip(ts, ts[1:]):
            consider(a, b)
        return max_quiet, scored

    def _price_series_has_gaps(
        self, path: Path, *, start: int, end: int, remote_has: bool | None
    ) -> bool:
        gap, _ = self._price_series_stats(
            path, name=path.name, start=start, end=end, remote_has=remote_has
        )
        return gap > _PRICE_STEP_MS

    def _trade_series_has_gaps(
        self, path: Path, *, start: int, end: int, remote_has: bool | None
    ) -> bool:
        gap, _ = self._trade_series_stats(
            path, name=path.name, start=start, end=end, remote_has=remote_has
        )
        return gap > _TRADE_GRADE_MS

    def _analyze_gaps(self, remote: dict[str, Any]) -> dict[str, Any]:
        """
        Grade from the worst single gap across 1s series + trade tapes
        (binance_trades / trades) — never a sum of gaps.
        """
        from app.core.live_dataset import (
            DATA_HEALTH_BAD,
            ORDERBOOKS_FILE,
            grade_data_health,
            grade_trade_health,
            resolve_orderbooks_path,
            worse_data_health,
        )

        date = str(remote.get("date") or "")
        mid = str(remote.get("market_id") or "")
        empty = {
            "max_gap_ms": 0,
            "max_trade_quiet_ms": 0,
            "notes": [],
            "notes_by_file": {},
            "comment": "",
            "grade": grade_data_health(0),
            "price_grade": grade_data_health(0),
            "trade_grade": grade_trade_health(0),
            "orderbooks_source": None,
        }
        if not date or not mid:
            return empty
        dest = self._local_dir(date, mid)
        if not dest.is_dir():
            note = f"market dir missing: {date}/{mid}"
            return {
                "max_gap_ms": 10**9,
                "max_trade_quiet_ms": 0,
                "notes": [note],
                "notes_by_file": {note: []},
                "comment": note,
                "grade": DATA_HEALTH_BAD,
                "orderbooks_source": None,
            }
        try:
            start = int(remote.get("start_time") or 0)
            end = int(remote.get("end_time") or 0)
        except (TypeError, ValueError):
            start = end = 0
        if start <= 0 or end <= start:
            try:
                meta = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
                start = int(meta.get("start_time") or 0)
                end = int(meta.get("end_time") or 0)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                note = "meta.json: invalid start/end time"
                return {
                    "max_gap_ms": 10**9,
                    "max_trade_quiet_ms": 0,
                    "notes": [note],
                    "notes_by_file": {"meta.json": ["invalid start/end time"]},
                    "comment": note,
                    "grade": DATA_HEALTH_BAD,
                    "orderbooks_source": None,
                }
        if start <= 0 or end <= start:
            note = "meta.json: invalid start/end time"
            return {
                "max_gap_ms": 10**9,
                "max_trade_quiet_ms": 0,
                "notes": [note],
                "notes_by_file": {"meta.json": ["invalid start/end time"]},
                "comment": note,
                "grade": DATA_HEALTH_BAD,
                "orderbooks_source": None,
            }
        now_ms = int(time.time() * 1000)
        if end > now_ms:
            end = now_ms
        if end <= start:
            return empty

        remote_names = self._remote_names(remote)
        local_only = not remote_names
        max_gap = 0
        max_trade_quiet = 0
        by_file: dict[str, list[tuple[int, str]]] = {}
        skip_pm_trade_quiet = False
        try:
            meta_skip = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
            skip_pm_trade_quiet = bool(meta_skip.get("trades_repaired_complete"))
        except (OSError, json.JSONDecodeError, TypeError):
            skip_pm_trade_quiet = False

        preferred_books = resolve_orderbooks_path(dest)
        orderbooks_source = preferred_books.name if preferred_books is not None else None
        for name in _PRICE_1S_FILES:
            step_ms: int | None = None
            if name == ORDERBOOKS_FILE:
                # Prefer pm_orderbooks.parquet when present; else live orderbooks.
                # Never score the gappy live orderbooks file when PM L2 exists.
                if preferred_books is not None:
                    path = preferred_books
                    name = preferred_books.name
                    remote_has = True
                    if name == "pm_orderbooks.parquet":
                        from app.core.pm_orderbooks import SLOT_MS as _PM_SLOT_MS

                        step_ms = int(_PM_SLOT_MS)
                else:
                    remote_has = True if local_only else (ORDERBOOKS_FILE in remote_names)
                    if not local_only and ORDERBOOKS_FILE not in remote_names:
                        continue
                    path = dest / ORDERBOOKS_FILE
            else:
                remote_has = True if local_only else (name in remote_names)
                if not local_only and name not in remote_names:
                    continue
                path = dest / name
            gap, file_notes = self._price_series_stats(
                path,
                name=name,
                start=start,
                end=end,
                remote_has=remote_has,
                step_ms=step_ms,
            )
            # Worst single hole in this file — never sum across holes.
            max_gap = max(max_gap, gap)
            if file_notes:
                by_file[name] = file_notes

        for name in _TRADE_FILES:
            remote_has = True if local_only else (name in remote_names)
            if not local_only and name not in remote_names:
                continue
            if name == "trades.parquet" and skip_pm_trade_quiet:
                continue
            quiet, file_notes = self._trade_series_stats(
                dest / name, name=name, start=start, end=end, remote_has=remote_has
            )
            # Worst single quiet in this tape — never sum.
            max_trade_quiet = max(max_trade_quiet, quiet)
            if file_notes:
                by_file[name] = file_notes

        notes_by_file, notes, comment = self._format_notes_by_file(by_file)

        # Separate scales: 1s series vs trade tapes; badge = worse of the two.
        price_grade = grade_data_health(max_gap)
        trade_grade = grade_trade_health(max_trade_quiet)
        return {
            "max_gap_ms": int(max_gap),
            "max_trade_quiet_ms": int(max_trade_quiet),
            "notes": notes,
            "notes_by_file": notes_by_file,
            "comment": comment,
            "price_grade": price_grade,
            "trade_grade": trade_grade,
            "grade": worse_data_health(price_grade, trade_grade),
            "orderbooks_source": orderbooks_source,
        }

    def _collect_gap_notes(self, remote: dict[str, Any]) -> list[str]:
        return list(self._analyze_gaps(remote).get("notes") or [])

    def _local_has_time_gaps(self, remote: dict[str, Any]) -> bool:
        """True when any price gap >1s or trade quiet ≥20s (repair candidate)."""
        analysis = self._analyze_gaps(remote)
        max_gap = int(analysis.get("max_gap_ms") or 0)
        max_trade = int(analysis.get("max_trade_quiet_ms") or 0)
        if max_gap > _PRICE_STEP_MS or max_trade > _TRADE_GRADE_MS:
            notes = analysis.get("notes") or []
            date = str(remote.get("date") or "")
            mid = str(remote.get("market_id") or "")
            if notes:
                logger.info("History gap: %s/%s — %s", date, mid, notes[0])
            return True
        return False

    def _remote_ahead(self, dest: Path, remote: dict[str, Any]) -> bool:
        """True when VPS catalog looks newer or has larger/missing-locally files."""
        remote_mtime = int(remote.get("mtime_ms") or 0)
        try:
            local_mtime = int((dest / "meta.json").stat().st_mtime * 1000)
        except OSError:
            return True
        if remote_mtime > local_mtime + 1_500:
            return True
        for f in remote.get("files") or []:
            if not isinstance(f, dict):
                continue
            name = str(f.get("name") or "")
            if not name:
                continue
            try:
                rsize = int(f.get("size") or 0)
            except (TypeError, ValueError):
                rsize = 0
            # pm_orderbooks satisfies the live orderbooks.parquet slot.
            if name in {"orderbooks.parquet", "pm_orderbooks.parquet"}:
                from app.core.live_dataset import resolve_orderbooks_path

                if resolve_orderbooks_path(dest) is not None:
                    continue
            p = dest / name
            if not p.is_file():
                return True
            try:
                if int(p.stat().st_size) < rsize:
                    return True
            except OSError:
                return True
        return False

    def _local_outcome_stale(self, dest: Path, remote: dict[str, Any]) -> bool:
        """True when VPS market is finished but local meta still lacks resolved outcome."""
        now_ms = int(time.time() * 1000)
        if not self._is_finished(remote, now_ms=now_ms):
            return False
        meta_path = dest / "meta.json"
        if not meta_path.is_file():
            return True
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        if not isinstance(meta, dict):
            return True
        if meta.get("closed") is True:
            return False
        if meta.get("winner") is not None:
            return False
        if meta.get("resolved_at") is not None:
            return False
        return True

    def _needs_pull(self, remote: dict[str, Any]) -> bool:
        date = str(remote.get("date") or "")
        mid = str(remote.get("market_id") or "")
        if not date or not mid:
            return False
        dest = self._local_dir(date, mid)
        if self._local_incomplete(dest, remote):
            return True
        if self._local_outcome_stale(dest, remote):
            return True
        if self._local_has_time_gaps(remote):
            # Gaps alone are not enough — only re-fetch if VPS can improve the copy.
            return self._remote_ahead(dest, remote)
        # Pull if VPS is clearly newer (collector still flushing / finalized)
        return self._remote_ahead(dest, remote)

    async def _post_pull_health(
        self,
        client: httpx.AsyncClient,
        remote: dict[str, Any],
        path: Path | None,
    ) -> Path | None:
        """
        After a VPS archive pull for a finished market:
        check gaps → force re-fetch if missed → Data API trade fill → persist health/comment.
        """
        if path is None:
            return None
        mid = str(remote.get("market_id") or path.name)
        now_ms = int(time.time() * 1000)
        if not self._is_finished(remote, now_ms=now_ms):
            return path

        local_path, stub = self._local_stub(mid)
        if local_path is None:
            local_path, stub = path, {
                "market_id": mid,
                "date": str(remote.get("date") or path.parent.name),
                "start_time": remote.get("start_time") or 0,
                "end_time": remote.get("end_time") or 0,
                "files": [],
            }

        gappy = self._local_has_time_gaps(stub) or self._trades_need_backfill(stub)
        if gappy:
            logger.info(
                "Missed data on %s after VPS pull — re-fetching archive + repairing",
                mid,
            )
            try:
                path2 = await self.download_market(client, remote)
                if path2 is not None:
                    path = path2
            except Exception as exc:
                self._log_net_error(f"health re-pull {mid}", exc)
            local_path, stub = self._local_stub(mid)
            if local_path is not None and self._trades_need_backfill(stub):
                try:
                    from app.core.trade_repair import backfill_trades_for_market_dir

                    added = await backfill_trades_for_market_dir(local_path)
                    if added:
                        logger.info(
                            "Post-resolve trade backfill %s (+%s rows)", mid, added
                        )
                    local_path, stub = self._local_stub(mid)
                except Exception as exc:
                    logger.warning("Post-resolve trade backfill failed %s: %s", mid, exc)

        if local_path is not None:
            self._persist_health(local_path, stub)
        return path

    async def download_market(
        self, client: httpx.AsyncClient, remote: dict[str, Any]
    ) -> Path | None:
        mid = str(remote.get("market_id") or "")
        date = str(remote.get("date") or "")
        if not mid:
            return None
        url = f"{self._base()}/markets/{mid}/archive"
        resp = await client.get(url)
        resp.raise_for_status()
        # Prefer date from catalog; fall back to zip date.txt or header
        if not date:
            date = (resp.headers.get("X-Market-Date") or "").strip()
        dest_parent = self.data_dir / (date or "_unknown")
        dest_parent.mkdir(parents=True, exist_ok=True)
        final = dest_parent / mid
        tmp = dest_parent / f".{mid}.sync.tmp"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                if not date:
                    try:
                        date_txt = zf.read("date.txt").decode("utf-8").strip()
                        if date_txt:
                            date = date_txt
                            dest_parent = self.data_dir / date
                            dest_parent.mkdir(parents=True, exist_ok=True)
                            final = dest_parent / mid
                            # move tmp under correct date if needed
                            new_tmp = dest_parent / f".{mid}.sync.tmp"
                            if new_tmp != tmp:
                                if new_tmp.exists():
                                    shutil.rmtree(new_tmp, ignore_errors=True)
                                shutil.move(str(tmp), str(new_tmp))
                                tmp = new_tmp
                    except KeyError:
                        pass
                for info in zf.infolist():
                    name = Path(info.filename).name
                    if name == "date.txt" or info.is_dir():
                        continue
                    if name not in _EXPECTED_FILES and not name.endswith(".parquet"):
                        if name != "meta.json":
                            continue
                    target = tmp / name
                    with zf.open(info) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)
            if not (tmp / "meta.json").is_file():
                raise RuntimeError(f"archive missing meta.json for {mid}")
            # Merge into the existing dir: overwrite VPS/archive files only.
            # Keep local-only artifacts (esp. pm_orderbooks.parquet) intact.
            if final.exists():
                from app.core.live_dataset import PM_ORDERBOOKS_FILE

                for src in tmp.iterdir():
                    if not src.is_file():
                        continue
                    # Never replace a local PM L2 book with an archive copy.
                    if src.name == PM_ORDERBOOKS_FILE:
                        existing = final / PM_ORDERBOOKS_FILE
                        if existing.is_file() and existing.stat().st_size > 0:
                            continue
                    shutil.copy2(src, final / src.name)
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                tmp.rename(final)
            logger.info("Synced market %s → %s", mid, final)
            return final
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def _is_finished(self, remote: dict[str, Any], *, now_ms: int) -> bool:
        if bool(remote.get("closed")):
            return True
        if remote.get("active") is False:
            return True
        try:
            end_ms = int(remote.get("end_time") or 0)
        except (TypeError, ValueError):
            end_ms = 0
        return end_ms > 0 and now_ms >= end_ms + 2_000

    async def sync_incremental(self) -> dict[str, Any]:
        """Pull finished markets with start_time > watermark. Never pulls the live window."""
        if not self.enabled:
            return {"enabled": False, "pulled": 0}
        if not self._base():
            return {"enabled": False, "pulled": 0}
        wm = self.load_watermark()
        after = int(wm.get("after_start_ms") or 0)
        pulled: list[str] = []
        now_ms = int(time.time() * 1000)
        new_after = after
        try:
            async with self._http() as client:
                markets = await self.list_markets(client, after_start_ms=after)
                self._unreachable = False
                for remote in markets:
                    mid = str(remote.get("market_id") or "")
                    if not mid:
                        continue
                    if not self._is_finished(remote, now_ms=now_ms):
                        # Skip in-progress live market — history sync only.
                        continue
                    try:
                        if self._needs_pull(remote):
                            path = await self.download_market(client, remote)
                            await self._post_pull_health(client, remote, path)
                            pulled.append(mid)
                        try:
                            st = int(remote.get("start_time") or 0)
                        except (TypeError, ValueError):
                            st = 0
                        if st > new_after:
                            new_after = st
                    except Exception as exc:
                        self._log_net_error(f"market {mid}", exc)
        except Exception as exc:
            self._log_net_error("incremental list", exc)
            return {
                "enabled": True,
                "pulled": 0,
                "error": str(exc),
                "after_start_ms": after,
            }
        if new_after != after or pulled:
            ids = list(wm.get("synced_market_ids") or [])
            for mid in pulled:
                if mid not in ids:
                    ids.append(mid)
            self.save_watermark(new_after, synced_ids=ids)
        return {
            "enabled": True,
            "pulled": len(pulled),
            "market_ids": pulled,
            "after_start_ms": new_after,
        }

    async def sync_tick(self) -> dict[str, Any]:
        """
        Periodic history update (default every 60s):
        - pull newly finished / newly resolved markets past the watermark
        - repair gaps; after each pull, re-check health and stamp meta.data_health
        Never downloads the currently live window.
        """
        if not self.enabled or not self._base():
            return {"enabled": False, "pulled": 0}
        inc = await self.sync_incremental()
        repaired: list[str] = []
        now_ms = int(time.time() * 1000)
        lookback_after = max(0, now_ms - _REPAIR_LOOKBACK_MS)
        try:
            async with self._http() as client:
                markets = await self.list_markets(client, after_start_ms=lookback_after)
                self._unreachable = False
                for remote in markets:
                    mid = str(remote.get("market_id") or "")
                    if not mid or not self._is_finished(remote, now_ms=now_ms):
                        continue
                    if mid in (inc.get("market_ids") or []):
                        continue
                    try:
                        if self._needs_pull(remote):
                            path = await self.download_market(client, remote)
                            await self._post_pull_health(client, remote, path)
                            repaired.append(mid)
                    except Exception as exc:
                        self._log_net_error(f"repair {mid}", exc)
        except Exception as exc:
            self._log_net_error("periodic repair list", exc)
            if not inc.get("error"):
                inc["error"] = str(exc)

        if repaired or int(inc.get("pulled") or 0) > 0:
            try:
                from app.core.live_dataset import TWAP_SPLIT
                from app.core.market_index import invalidate_market_index

                invalidate_market_index(TWAP_SPLIT)
            except Exception:
                pass
        return {
            **inc,
            "repaired": len(repaired),
            "repaired_ids": repaired,
        }

    async def pull_market(
        self, market_id: str, *, force: bool = False
    ) -> Path | None:
        """Download one market by id (closed rollover or mid-window refresh)."""
        if not self.enabled or not market_id or not self._base():
            return None
        try:
            async with self._http(read_s=120.0 if force else 20.0) as client:
                remote = await self.get_market(client, market_id)
                if remote is None:
                    logger.warning("VPS market not found: %s", market_id)
                    return None
                self._unreachable = False
                if not force and not self._needs_pull(remote):
                    date = str(remote.get("date") or "")
                    return self._local_dir(date, market_id)
                path = await self.download_market(client, remote)
                return await self._post_pull_health(client, remote, path)
        except Exception as exc:
            self._log_net_error(f"pull {market_id}", exc)
            return None

    async def request_vps_repair(self, market_id: str) -> dict[str, Any]:
        """Ask fetch_live serve to merge Data API trades into VPS trades.parquet."""
        if not self.enabled or not self._base():
            return {"ok": False, "error": "VPS sync is not configured"}
        mid = str(market_id or "").strip()
        if not mid:
            return {"ok": False, "error": "missing market_id"}
        try:
            async with self._http(read_s=120.0) as client:
                remote = await self.get_market(client, mid)
                url = f"{self._base()}/markets/{mid}/repair"
                resp = await client.post(url)
                if resp.status_code == 404:
                    if remote is None:
                        return {"ok": False, "error": f"VPS has no market {mid}"}
                    return {
                        "ok": False,
                        "endpoint_missing": True,
                        "error": (
                            "VPS serve is old (no POST /repair). "
                            "On the VPS: git pull, then pm2 restart fetch-live-serve"
                        ),
                    }
                if resp.status_code >= 400:
                    detail = ""
                    try:
                        body = resp.json()
                        if isinstance(body, dict):
                            detail = str(body.get("detail") or body.get("error") or "")
                    except Exception:
                        detail = (resp.text or "")[:240]
                    return {
                        "ok": False,
                        "error": detail or f"VPS repair HTTP {resp.status_code}",
                        "status_code": resp.status_code,
                    }
                data = resp.json()
                self._unreachable = False
                return data if isinstance(data, dict) else {"ok": True}
        except Exception as exc:
            self._log_net_error(f"repair {market_id}", exc)
            return {"ok": False, "error": str(exc)}

    async def repair_history_market(self, market_id: str) -> dict[str, Any]:
        """
        Repair missed trades on the VPS, re-pull the archive, then restamp health.
        Used by the history-market Repair button.
        """
        from app.core.live_dataset import (
            TWAP_SPLIT,
            read_data_health,
            read_data_health_comment,
            _read_meta,
        )
        from app.core.market_index import invalidate_market_index

        mid = str(market_id or "").strip()
        if not mid:
            return {"ok": False, "error": "missing market_id"}

        async with self._hist_lock(mid):
            vps_enabled = bool(self.enabled and self._base())
            vps_repair: dict[str, Any] = {"ok": False, "skipped": True}
            if vps_enabled:
                vps_repair = await self.request_vps_repair(mid)
                vps_repair["skipped"] = False

            pulled = False
            if vps_enabled:
                path = await self.pull_market(mid, force=True)
                pulled = path is not None

            local_path, stub = self._local_stub(mid)
            if local_path is None:
                return {
                    "ok": False,
                    "market_id": mid,
                    "pulled": pulled,
                    "vps_enabled": vps_enabled,
                    "vps_repair": vps_repair,
                    "error": vps_repair.get("error") or "local market not found",
                }

            trade_added = int(vps_repair.get("rows_added") or 0)
            local_filled: dict[str, int] = {}
            # Always fill locally on explicit Repair (covers old VPS serve / API lag).
            try:
                from app.core.trade_repair import backfill_trades_for_market_dir

                local_added = int(await backfill_trades_for_market_dir(local_path) or 0)
                trade_added += local_added
                local_filled["trades.parquet"] = local_added
            except Exception as exc:
                logger.warning("Repair local trade backfill failed for %s: %s", mid, exc)
            try:
                from app.core.series_repair import repair_series_for_market_dir

                series_filled = await repair_series_for_market_dir(local_path)
                local_filled.update(series_filled or {})
            except Exception as exc:
                logger.warning("Repair local series backfill failed for %s: %s", mid, exc)
            local_path, stub = self._local_stub(mid)

            if local_path is None:
                return {
                    "ok": False,
                    "market_id": mid,
                    "pulled": pulled,
                    "vps_enabled": vps_enabled,
                    "vps_repair": vps_repair,
                    "error": "local market missing after repair",
                }

            grade = self._persist_health(local_path, stub) or "unchecked"
            meta = _read_meta(local_path / "meta.json") or {}
            try:
                invalidate_market_index(TWAP_SPLIT)
            except Exception:
                pass

            analysis = self._analyze_gaps(stub)
            vps_ok = bool(vps_repair.get("ok"))
            endpoint_missing = bool(vps_repair.get("endpoint_missing"))
            warning = None
            if vps_enabled and not vps_ok:
                warning = str(vps_repair.get("error") or "VPS repair failed")
            # Succeed if VPS repaired, or we filled locally while serve is only outdated.
            local_added_total = sum(int(v or 0) for v in local_filled.values())
            ok = (
                vps_ok
                or (not vps_enabled)
                or endpoint_missing
                or trade_added > 0
                or local_added_total > 0
            )
            filled = dict(vps_repair.get("filled") or {})
            for name, n in local_filled.items():
                filled[name] = int(filled.get(name) or 0) + int(n or 0)
            return {
                "ok": ok,
                "market_id": mid,
                "pulled": pulled,
                "vps_enabled": vps_enabled,
                "vps_repair": vps_repair,
                "filled": filled,
                "trade_rows_added": trade_added,
                "data_health": grade or read_data_health(meta),
                "data_health_comment": read_data_health_comment(meta),
                "max_gap_ms": int(analysis.get("max_gap_ms") or 0),
                "max_trade_quiet_ms": int(analysis.get("max_trade_quiet_ms") or 0),
                "notes": list(analysis.get("notes") or []),
                "notes_by_file": dict(analysis.get("notes_by_file") or {}),
                "orderbooks_source": analysis.get("orderbooks_source"),
                "warning": warning if ok else None,
                "error": None if ok else warning,
            }

    async def recheck_history_market(self, market_id: str) -> dict[str, Any]:
        """
        Force VPS re-pull (when enabled), optional Data API trade fill,
        then rewrite meta.data_health + comment. Used by the health badge dialog.
        """
        from app.core.live_dataset import (
            TWAP_SPLIT,
            read_data_health,
            read_data_health_comment,
            _read_meta,
        )
        from app.core.market_index import invalidate_market_index

        mid = str(market_id or "").strip()
        if not mid:
            return {"ok": False, "error": "missing market_id"}

        async with self._hist_lock(mid):
            pulled = False
            vps_enabled = bool(self.enabled and self._base())
            if vps_enabled:
                path = await self.pull_market(mid, force=True)
                pulled = path is not None

            local_path, stub = self._local_stub(mid)
            if local_path is None:
                return {
                    "ok": False,
                    "market_id": mid,
                    "pulled": pulled,
                    "vps_enabled": vps_enabled,
                    "error": "local market not found",
                }

            trade_added = 0
            if self._trades_need_backfill(stub):
                try:
                    from app.core.trade_repair import backfill_trades_for_market_dir

                    trade_added = int(await backfill_trades_for_market_dir(local_path) or 0)
                    local_path, stub = self._local_stub(mid)
                except Exception as exc:
                    logger.warning("Recheck trade backfill failed for %s: %s", mid, exc)

            if local_path is None:
                return {
                    "ok": False,
                    "market_id": mid,
                    "pulled": pulled,
                    "vps_enabled": vps_enabled,
                    "error": "local market missing after repair",
                }

            grade = self._persist_health(local_path, stub) or "unchecked"
            meta = _read_meta(local_path / "meta.json") or {}
            try:
                invalidate_market_index(TWAP_SPLIT)
            except Exception:
                pass

            analysis = self._analyze_gaps(stub)
            return {
                "ok": True,
                "market_id": mid,
                "pulled": pulled,
                "vps_enabled": vps_enabled,
                "trade_rows_added": trade_added,
                "data_health": grade or read_data_health(meta),
                "data_health_comment": read_data_health_comment(meta),
                "max_gap_ms": int(analysis.get("max_gap_ms") or 0),
                "max_trade_quiet_ms": int(analysis.get("max_trade_quiet_ms") or 0),
                "notes": list(analysis.get("notes") or []),
                "notes_by_file": dict(analysis.get("notes_by_file") or {}),
                "orderbooks_source": analysis.get("orderbooks_source"),
            }

    def _iter_local_market_dirs(self) -> list[Path]:
        root = self.data_dir
        if not root.is_dir():
            return []
        out: list[Path] = []
        for day in sorted(p for p in root.iterdir() if p.is_dir() and _DAY_RE.match(p.name)):
            for mid_dir in sorted(p for p in day.iterdir() if p.is_dir()):
                if (mid_dir / "meta.json").is_file():
                    out.append(mid_dir)
        return out

    async def refresh_local_health(
        self,
        *,
        force_pull: bool = True,
        only_badged: bool = True,
    ) -> dict[str, Any]:
        """
        Re-pull from VPS (optional) and rewrite data_health + comments for local markets.
        only_badged=True → markets that already have a Great→Bad (or legacy) stamp.
        """
        from app.core.live_dataset import (
            TWAP_SPLIT,
            is_data_health_checked,
            read_data_health,
            _read_meta,
        )
        from app.core.market_index import invalidate_market_index

        dirs = self._iter_local_market_dirs()
        targets: list[Path] = []
        for d in dirs:
            meta = _read_meta(d / "meta.json") or {}
            raw = str(meta.get("data_health") or "").strip().lower()
            health = read_data_health(meta)
            badged = is_data_health_checked(health) or raw in {"healthy", "unhealthy"}
            if only_badged and not badged:
                continue
            targets.append(d)

        pulled: list[str] = []
        updated: list[dict[str, str]] = []
        errors: list[str] = []

        for d in targets:
            mid = d.name
            try:
                path: Path | None = d
                if force_pull and self.enabled and self._base():
                    got = await self.pull_market(mid, force=True)
                    if got is not None:
                        path = got
                        pulled.append(mid)
                local_path, stub = self._local_stub(mid)
                if local_path is None:
                    local_path, stub = path, {
                        "market_id": mid,
                        "date": path.parent.name if path else d.parent.name,
                        "start_time": 0,
                        "end_time": 0,
                        "files": [],
                    }
                    # Prefer fresh stub times from meta after pull.
                    local_path, stub = self._local_stub(mid)
                if local_path is None:
                    errors.append(f"{mid}: missing local dir")
                    continue
                if self._trades_need_backfill(stub):
                    try:
                        from app.core.trade_repair import backfill_trades_for_market_dir

                        await backfill_trades_for_market_dir(local_path)
                        local_path, stub = self._local_stub(mid)
                    except Exception as exc:
                        logger.warning("Trade backfill failed for %s: %s", mid, exc)
                if local_path is None:
                    errors.append(f"{mid}: missing after backfill")
                    continue
                grade = self._persist_health(local_path, stub) or "unchecked"
                updated.append({"market_id": mid, "data_health": grade})
            except Exception as exc:
                errors.append(f"{mid}: {exc}")
                logger.warning("refresh_local_health %s failed: %s", mid, exc)

        try:
            invalidate_market_index(TWAP_SPLIT)
        except Exception:
            pass

        return {
            "targets": len(targets),
            "pulled": len(pulled),
            "updated": len(updated),
            "market_ids": [u["market_id"] for u in updated],
            "grades": updated,
            "errors": errors,
        }

    async def ensure_active_market(
        self, market_id: str, *, force: bool = False
    ) -> Path | None:
        """Pull in-progress market history from VPS (throttled) for live chart backfill."""
        if not self.enabled or not market_id or not self._base():
            return None
        now = time.monotonic()
        last = self._last_active_pull.get(market_id, 0.0)
        # Always throttle — even force — so live seed + soft reconnect don't spam VPS.
        if (now - last) < _ACTIVE_REFRESH_S:
            return None
        if self._unreachable and (now - last) < 60.0:
            return None
        self._last_active_pull[market_id] = now
        # force=True means "refresh live prefix if VPS is ahead"; still skip when local is current.
        return await self.pull_market(market_id, force=False)

    def _local_stub(self, market_id: str) -> tuple[Path | None, dict[str, Any]]:
        from app.core.live_dataset import find_live_market_dir

        mid = str(market_id).strip()
        d = find_live_market_dir(mid)
        if d is None:
            return None, {
                "market_id": mid,
                "date": "",
                "start_time": 0,
                "end_time": 0,
                "files": [],
            }
        start = end = 0
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            start = int(meta.get("start_time") or 0)
            end = int(meta.get("end_time") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
        files: list[dict[str, Any]] = []
        try:
            for p in d.iterdir():
                if p.is_file():
                    try:
                        files.append({"name": p.name, "size": int(p.stat().st_size)})
                    except OSError:
                        files.append({"name": p.name, "size": 0})
        except OSError:
            pass
        return d, {
            "market_id": mid,
            "date": d.parent.name,
            "start_time": start,
            "end_time": end,
            "files": files,
        }

    def _trades_need_backfill(self, remote_or_stub: dict[str, Any]) -> bool:
        """True when Polymarket trades.parquet has ≥10s silence (or is missing)."""
        date = str(remote_or_stub.get("date") or "")
        mid = str(remote_or_stub.get("market_id") or "")
        if not date or not mid:
            return False
        dest = self._local_dir(date, mid)
        try:
            start = int(remote_or_stub.get("start_time") or 0)
            end = int(remote_or_stub.get("end_time") or 0)
        except (TypeError, ValueError):
            start = end = 0
        if start <= 0 or end <= start:
            return False
        now_ms = int(time.time() * 1000)
        if end > now_ms:
            end = now_ms
        if end <= start:
            return False
        remote_names = self._remote_names(remote_or_stub)
        remote_has: bool | None = True if not remote_names else ("trades.parquet" in remote_names)
        return self._trade_series_has_gaps(
            dest / "trades.parquet", start=start, end=end, remote_has=remote_has
        )

    def _evaluate_local_health(self, stub: dict[str, Any]) -> tuple[str, str]:
        """Return (great|good|ok|low|bad, comment grouped by file)."""
        from app.core.live_dataset import DATA_HEALTH_GREAT

        analysis = self._analyze_gaps(stub)
        grade = str(analysis.get("grade") or DATA_HEALTH_GREAT)
        if grade == DATA_HEALTH_GREAT:
            return grade, ""
        comment = str(analysis.get("comment") or "").strip()
        if not comment:
            notes = list(analysis.get("notes") or [])
            comment = "\n".join(str(n) for n in notes if n)
        return grade, comment

    def _persist_health(self, market_dir: Path | None, stub: dict[str, Any]) -> str | None:
        if market_dir is None or not market_dir.is_dir():
            return None
        from app.core.live_dataset import TWAP_SPLIT, write_data_health
        from app.core.market_index import invalidate_market_index

        status, comment = self._evaluate_local_health(stub)
        written = write_data_health(market_dir, status, comment=comment)
        try:
            invalidate_market_index(TWAP_SPLIT)
        except Exception:
            pass
        logger.info(
            "Marked market %s data_health=%s%s",
            market_dir.name,
            written,
            f" ({comment.splitlines()[0]})" if comment else "",
        )
        return written

    async def ensure_history_market(self, market_id: str) -> Path | None:
        """
        On history market switch (only when meta.data_health is unchecked):
        1) If local is incomplete/gappy and VPS is ahead → redownload archive
        2) If trades.parquet still has ≥10s gaps → merge Data API trades
        3) Persist data_health=great|good|ok|low|bad on meta.json (skips future VPS checks)
        """
        from app.core.live_dataset import (
            DATA_HEALTH_GREAT,
            is_data_health_checked,
            read_data_health,
            read_data_health_comment,
            _read_meta,
        )

        mid = str(market_id or "").strip()
        if not mid:
            return None

        async with self._hist_lock(mid):
            local_path, stub = self._local_stub(mid)
            if local_path is not None:
                meta = _read_meta(local_path / "meta.json") or {}
                health = read_data_health(meta)
                if is_data_health_checked(health):
                    # Already graded — never re-fetch from VPS / Data API on select.
                    # Refresh when comment missing, legacy labels, or grade formula changed.
                    raw = str(meta.get("data_health") or "").strip().lower()
                    needs_comment = health != DATA_HEALTH_GREAT and not read_data_health_comment(
                        meta
                    )
                    legacy = raw in {"healthy", "unhealthy"}
                    fresh, _ = self._evaluate_local_health(stub)
                    if needs_comment or legacy or fresh != health:
                        self._persist_health(local_path, stub)
                    return local_path

            now = time.monotonic()
            last = self._last_active_pull.get(f"hist:{mid}", 0.0)
            local_gappy = local_path is None or self._local_has_time_gaps(stub)

            # --- VPS pull (outcome / gaps) + health stamp ---
            pulled = False
            if self.enabled and self._base() and not (
                self._unreachable and (now - last) < 60.0
            ):
                try:
                    async with self._http() as client:
                        remote = await self.get_market(client, mid)
                        if remote is None:
                            logger.info("VPS has no market %s — keeping local", mid)
                        else:
                            self._unreachable = False
                            if self._needs_pull(remote):
                                path = await self.download_market(client, remote)
                                path = await self._post_pull_health(client, remote, path)
                                if path is not None:
                                    local_path = path
                                    pulled = True
                                    logger.info(
                                        "Repaired local history market %s from VPS",
                                        mid,
                                    )
                            elif local_gappy:
                                logger.info(
                                    "History gaps on %s but VPS archive matches local "
                                    "— skipping re-download",
                                    mid,
                                )
                except Exception as exc:
                    self._log_net_error(f"history ensure {mid}", exc)

            # If we didn't pull (no VPS / already current), still trade-fill + stamp health.
            if local_path is not None and not pulled:
                local_path, stub = self._local_stub(mid)
                if self._trades_need_backfill(stub):
                    try:
                        from app.core.trade_repair import backfill_trades_for_market_dir

                        added = await backfill_trades_for_market_dir(local_path)
                        if added:
                            logger.info(
                                "Updated local trades for %s (+%s rows from Data API)",
                                mid,
                                added,
                            )
                        local_path, stub = self._local_stub(mid)
                    except Exception as exc:
                        logger.warning("Trade backfill failed for %s: %s", mid, exc)
                self._persist_health(local_path, stub)

            self._last_active_pull[f"hist:{mid}"] = time.monotonic()
            if local_path is not None:
                return local_path
            date = str(stub.get("date") or "")
            return self._local_dir(date, mid) if date else None


_client: VpsSyncClient | None = None


def get_vps_sync() -> VpsSyncClient:
    global _client
    if _client is None:
        _client = VpsSyncClient()
    return _client
