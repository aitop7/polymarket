from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.storage.market_sessions import sessions
from app.storage.parquet_store import store
from app.utils.time import utcnow


@dataclass
class MarketRecord:
    market_id: str
    slug: str
    condition_id: str | None = None
    token_yes: str | None = None
    token_no: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    settlement_time: datetime | None = None
    opening_btc_price: float | None = None
    closing_btc_price: float | None = None
    winner: str | bool | None = None
    status: str = "active"
    raw_json: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> MarketRecord:
        def _dt(value: Any) -> datetime | None:
            if value is None or (isinstance(value, float) and pd_isna(value)):
                return None
            if isinstance(value, datetime):
                return value
            if isinstance(value, (int, float)):
                from app.utils.time import ms_to_datetime

                v = int(value)
                return ms_to_datetime(v if v > 10_000_000_000 else v * 1000)
            text = str(value)
            if text in {"", "None", "NaT", "nan"}:
                return None
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None

        import json

        raw = row.get("raw_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {"raw": raw}

        winner = row.get("winner")
        open_px = row.get("btc_open_price", row.get("opening_btc_price"))
        close_px = row.get("btc_close_price", row.get("closing_btc_price"))
        resolved = row.get("resolved_at", row.get("settlement_time"))

        return cls(
            market_id=str(row.get("market_id") or ""),
            slug=str(row.get("slug") or ""),
            condition_id=_optional_str(row.get("condition_id")),
            token_yes=_optional_str(row.get("token_yes")),
            token_no=_optional_str(row.get("token_no")),
            start_time=_dt(row.get("start_time")),
            end_time=_dt(row.get("end_time")),
            settlement_time=_dt(resolved),
            opening_btc_price=_optional_float(open_px),
            closing_btc_price=_optional_float(close_px),
            winner=winner if isinstance(winner, bool) else _optional_str(winner),
            status=str(row.get("status") or "active"),
            raw_json=raw if isinstance(raw, dict) else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "slug": self.slug,
            "condition_id": self.condition_id,
            "token_yes": self.token_yes,
            "token_no": self.token_no,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "settlement_time": self.settlement_time,
            "resolved_at": self.settlement_time,
            "opening_btc_price": self.opening_btc_price,
            "closing_btc_price": self.closing_btc_price,
            "btc_open_price": self.opening_btc_price,
            "btc_close_price": self.closing_btc_price,
            "winner": self.winner,
            "status": self.status,
            "raw_json": self.raw_json,
        }


def pd_isna(value: Any) -> bool:
    try:
        import pandas as pd

        return bool(pd.isna(value))
    except Exception:
        return False


def _optional_str(value: Any) -> str | None:
    if value is None or pd_isna(value):
        return None
    text = str(value)
    return None if text in {"", "None", "nan"} else text


def _optional_float(value: Any) -> float | None:
    if value is None or pd_isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MarketRegistry:
    """In-memory + snapshot registry; finalizes one parquet file per closed market."""

    def __init__(self) -> None:
        self._markets: dict[str, MarketRecord] = {}
        self.reload()

    def reload(self) -> None:
        rows = store.read_snapshot("markets_latest")
        self._markets = {
            row["market_id"]: MarketRecord.from_dict(row)
            for row in rows
            if row.get("market_id")
        }

    def upsert_many(self, rows: list[dict[str, Any]], *, finalize_closed: bool = True) -> int:
        newly_closed: list[str] = []
        for row in rows:
            rec = MarketRecord.from_dict(row)
            if not rec.market_id:
                continue
            prev = self._markets.get(rec.market_id)
            self._markets[rec.market_id] = rec
            sessions.set_market_meta(rec.as_dict())
            if finalize_closed and rec.status in {"closed", "resolved", "inactive"}:
                if prev is None or prev.status in {"active", "open"}:
                    newly_closed.append(rec.market_id)

        self._persist()
        if newly_closed:
            sessions.finalize_closed(newly_closed)
        return len(rows)

    def upsert_one(self, row: dict[str, Any]) -> None:
        self.upsert_many([row])

    def list_active(self) -> list[MarketRecord]:
        now = utcnow()
        out = []
        for m in self._markets.values():
            if m.status not in {"active", "open"}:
                continue
            # auto-close by end_time for 5m markets
            end = m.end_time or m.settlement_time
            if end is not None and now > end:
                continue
            out.append(m)
        return out

    def list_expired_active(self) -> list[MarketRecord]:
        """Active in registry but past end_time — ready to finalize."""
        now = utcnow()
        out = []
        for m in self._markets.values():
            if m.status not in {"active", "open"}:
                continue
            end = m.end_time or m.settlement_time
            if end is not None and now > end:
                out.append(m)
        return out

    def get(self, market_id: str) -> MarketRecord | None:
        return self._markets.get(market_id)

    def all(self) -> list[MarketRecord]:
        return list(self._markets.values())

    def _persist(self) -> None:
        store.write_snapshot("markets_latest", [m.as_dict() for m in self._markets.values()])


markets = MarketRegistry()
