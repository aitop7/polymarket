"""Pull fetch_live market dirs from VPS serve API into local FETCH_LIVE_DATA_DIR."""

from __future__ import annotations

import io
import json
import logging
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache"
_WATERMARK_PATH = _CACHE_DIR / "fetch_live_sync.json"
_ACTIVE_REFRESH_S = 12.0
_EXPECTED_FILES = (
    "meta.json",
    "binance_trades.parquet",
    "binance_price_orderbook.parquet",
    "chainlink_price.parquet",
    "orderbooks.parquet",
    "trades.parquet",
)


class VpsSyncClient:
    def __init__(self) -> None:
        self._last_active_pull: dict[str, float] = {}
        self._last_error_log = 0.0
        self._unreachable = False

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

    def _http(self) -> httpx.AsyncClient:
        # Connect/read short enough to fail fast when port accepts then resets.
        return httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0, read=20.0),
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
            return False
        for name, size in remote_files.items():
            p = dest / name
            if not p.is_file():
                return True
            try:
                if int(p.stat().st_size) < size:
                    return True
            except OSError:
                return True
        return False

    def _needs_pull(self, remote: dict[str, Any]) -> bool:
        date = str(remote.get("date") or "")
        mid = str(remote.get("market_id") or "")
        if not date or not mid:
            return False
        dest = self._local_dir(date, mid)
        if self._local_incomplete(dest, remote):
            return True
        remote_mtime = int(remote.get("mtime_ms") or 0)
        if remote_mtime <= 0:
            return False
        try:
            local_mtime = int((dest / "meta.json").stat().st_mtime * 1000)
        except OSError:
            return True
        # Pull if VPS is clearly newer (collector still flushing / finalized)
        return remote_mtime > local_mtime + 1_500

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
            if final.exists():
                shutil.rmtree(final, ignore_errors=True)
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
        """Pull all markets with start_time > watermark; advance watermark for finished ones."""
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
                    try:
                        if self._needs_pull(remote):
                            await self.download_market(client, remote)
                            pulled.append(mid)
                        if self._is_finished(remote, now_ms=now_ms):
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

    async def pull_market(
        self, market_id: str, *, force: bool = False
    ) -> Path | None:
        """Download one market by id (closed rollover or mid-window refresh)."""
        if not self.enabled or not market_id or not self._base():
            return None
        try:
            async with self._http() as client:
                remote = await self.get_market(client, market_id)
                if remote is None:
                    logger.warning("VPS market not found: %s", market_id)
                    return None
                self._unreachable = False
                if not force and not self._needs_pull(remote):
                    date = str(remote.get("date") or "")
                    return self._local_dir(date, market_id)
                return await self.download_market(client, remote)
        except Exception as exc:
            self._log_net_error(f"pull {market_id}", exc)
            return None

    async def ensure_active_market(
        self, market_id: str, *, force: bool = False
    ) -> Path | None:
        """Throttle mid-window pulls so local has the VPS prefix for the current market."""
        if not self.enabled or not market_id or not self._base():
            return None
        now = time.monotonic()
        last = self._last_active_pull.get(market_id, 0.0)
        if not force and (now - last) < _ACTIVE_REFRESH_S:
            return None
        # Back off harder while VPS is known unreachable
        if self._unreachable and not force and (now - last) < 60.0:
            return None
        self._last_active_pull[market_id] = now
        return await self.pull_market(market_id, force=force)


_client: VpsSyncClient | None = None


def get_vps_sync() -> VpsSyncClient:
    global _client
    if _client is None:
        _client = VpsSyncClient()
    return _client
