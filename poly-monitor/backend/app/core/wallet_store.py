"""Local SQLite cache for watched wallets (summary, daily, pnl, day activity)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.core.config import POLY_MONITOR_ROOT

_DB_PATH = POLY_MONITOR_ROOT / "data" / "wallets.sqlite3"
_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS watched_wallets (
          address TEXT PRIMARY KEY,
          name TEXT,
          profile_image TEXT,
          positions_value REAL,
          total_pnl REAL,
          summary_json TEXT NOT NULL,
          daily_json TEXT,
          updated_at INTEGER NOT NULL,
          last_viewed_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS wallet_pnl_cache (
          address TEXT NOT NULL,
          interval TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          updated_at INTEGER NOT NULL,
          PRIMARY KEY (address, interval)
        );

        CREATE TABLE IF NOT EXISTS wallet_day_cache (
          address TEXT NOT NULL,
          date TEXT NOT NULL,
          markets_json TEXT,
          activity_json TEXT,
          updated_at INTEGER NOT NULL,
          PRIMARY KEY (address, date)
        );
        """
    )
    # Lightweight migrations for existing DBs.
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(watched_wallets)").fetchall()}
    if "comment" not in cols:
        conn.execute("ALTER TABLE watched_wallets ADD COLUMN comment TEXT")
    conn.commit()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    return json.loads(raw)


def list_watched_wallets() -> list[dict[str, Any]]:
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            rows = conn.execute(
                """
                SELECT address, name, profile_image, positions_value, total_pnl,
                       comment, updated_at, last_viewed_at
                FROM watched_wallets
                ORDER BY last_viewed_at DESC
                """
            ).fetchall()
        finally:
            conn.close()
    return [
        {
            "wallet": r["address"],
            "name": r["name"],
            "profile_image": r["profile_image"],
            "positions_value": r["positions_value"],
            "total_pnl": r["total_pnl"],
            "comment": r["comment"] or "",
            "updated_at": r["updated_at"],
            "last_viewed_at": r["last_viewed_at"],
        }
        for r in rows
    ]


def touch_viewed(address: str) -> None:
    addr = address.lower()
    now = _now_ms()
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            conn.execute(
                "UPDATE watched_wallets SET last_viewed_at=? WHERE address=?",
                (now, addr),
            )
            conn.commit()
        finally:
            conn.close()


def get_comment(address: str) -> str:
    addr = address.lower()
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute(
                "SELECT comment FROM watched_wallets WHERE address=?", (addr,)
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return ""
    return str(row["comment"] or "")


def set_comment(address: str, comment: str) -> str:
    """Save a trader note. Creates a watched row if needed."""
    addr = address.lower()
    text = (comment or "").strip()
    # Keep notes bounded for local UI use.
    if len(text) > 4000:
        text = text[:4000]
    now = _now_ms()
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            existing = conn.execute(
                "SELECT address FROM watched_wallets WHERE address=?", (addr,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE watched_wallets SET comment=?, updated_at=? WHERE address=?",
                    (text, now, addr),
                )
            else:
                stub = {"wallet": addr, "name": addr}
                conn.execute(
                    """
                    INSERT INTO watched_wallets (
                      address, name, profile_image, positions_value, total_pnl,
                      summary_json, daily_json, comment, updated_at, last_viewed_at
                    ) VALUES (?, ?, NULL, NULL, NULL, ?, NULL, ?, ?, ?)
                    """,
                    (addr, addr, _dumps(stub), text, now, now),
                )
            conn.commit()
        finally:
            conn.close()
    return text


def delete_watched_wallet(address: str) -> bool:
    addr = address.lower()
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            cur = conn.execute("DELETE FROM watched_wallets WHERE address=?", (addr,))
            conn.execute("DELETE FROM wallet_pnl_cache WHERE address=?", (addr,))
            conn.execute("DELETE FROM wallet_day_cache WHERE address=?", (addr,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def save_summary(address: str, summary: dict[str, Any], daily: dict[str, Any] | None = None) -> None:
    addr = address.lower()
    now = _now_ms()
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            existing = conn.execute(
                "SELECT last_viewed_at FROM watched_wallets WHERE address=?", (addr,)
            ).fetchone()
            last_viewed = int(existing["last_viewed_at"]) if existing else now
            conn.execute(
                """
                INSERT INTO watched_wallets (
                  address, name, profile_image, positions_value, total_pnl,
                  summary_json, daily_json, updated_at, last_viewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                  name=excluded.name,
                  profile_image=excluded.profile_image,
                  positions_value=excluded.positions_value,
                  total_pnl=excluded.total_pnl,
                  summary_json=excluded.summary_json,
                  daily_json=COALESCE(excluded.daily_json, watched_wallets.daily_json),
                  updated_at=excluded.updated_at,
                  last_viewed_at=excluded.last_viewed_at
                """,
                (
                    addr,
                    summary.get("name"),
                    summary.get("profile_image"),
                    summary.get("positions_value"),
                    summary.get("total_pnl"),
                    _dumps(summary),
                    _dumps(daily) if daily is not None else None,
                    now,
                    now if not existing else last_viewed,
                ),
            )
            # Always bump last_viewed when saving from a watch
            conn.execute(
                "UPDATE watched_wallets SET last_viewed_at=? WHERE address=?",
                (now, addr),
            )
            conn.commit()
        finally:
            conn.close()


def save_daily(address: str, daily: dict[str, Any]) -> None:
    addr = address.lower()
    now = _now_ms()
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            conn.execute(
                """
                UPDATE watched_wallets
                SET daily_json=?, updated_at=?, last_viewed_at=?
                WHERE address=?
                """,
                (_dumps(daily), now, now, addr),
            )
            conn.commit()
        finally:
            conn.close()


def get_summary(address: str) -> dict[str, Any] | None:
    addr = address.lower()
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute(
                "SELECT summary_json, comment FROM watched_wallets WHERE address=?",
                (addr,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    data = _loads(row["summary_json"])
    if isinstance(data, dict):
        data = {**data, "cached": True, "comment": row["comment"] or ""}
    return data


def get_daily(address: str) -> dict[str, Any] | None:
    addr = address.lower()
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute(
                "SELECT daily_json FROM watched_wallets WHERE address=?", (addr,)
            ).fetchone()
        finally:
            conn.close()
    if not row or not row["daily_json"]:
        return None
    data = _loads(row["daily_json"])
    if isinstance(data, dict):
        data = {**data, "cached": True}
    return data


def save_pnl(address: str, interval: str, payload: dict[str, Any]) -> None:
    addr = address.lower()
    now = _now_ms()
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            conn.execute(
                """
                INSERT INTO wallet_pnl_cache (address, interval, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(address, interval) DO UPDATE SET
                  payload_json=excluded.payload_json,
                  updated_at=excluded.updated_at
                """,
                (addr, interval.lower(), _dumps(payload), now),
            )
            conn.commit()
        finally:
            conn.close()


def get_pnl(address: str, interval: str) -> dict[str, Any] | None:
    addr = address.lower()
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute(
                """
                SELECT payload_json FROM wallet_pnl_cache
                WHERE address=? AND interval=?
                """,
                (addr, interval.lower()),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    data = _loads(row["payload_json"])
    if isinstance(data, dict):
        data = {**data, "cached": True}
    return data


def save_day(
    address: str,
    date: str,
    *,
    markets: dict[str, Any] | None = None,
    activity: dict[str, Any] | None = None,
) -> None:
    addr = address.lower()
    now = _now_ms()
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            existing = conn.execute(
                "SELECT markets_json, activity_json FROM wallet_day_cache WHERE address=? AND date=?",
                (addr, date),
            ).fetchone()
            markets_json = (
                _dumps(markets)
                if markets is not None
                else (existing["markets_json"] if existing else None)
            )
            activity_json = (
                _dumps(activity)
                if activity is not None
                else (existing["activity_json"] if existing else None)
            )
            conn.execute(
                """
                INSERT INTO wallet_day_cache (address, date, markets_json, activity_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(address, date) DO UPDATE SET
                  markets_json=excluded.markets_json,
                  activity_json=excluded.activity_json,
                  updated_at=excluded.updated_at
                """,
                (addr, date, markets_json, activity_json, now),
            )
            conn.commit()
        finally:
            conn.close()


def get_day_markets(address: str, date: str) -> dict[str, Any] | None:
    addr = address.lower()
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute(
                "SELECT markets_json FROM wallet_day_cache WHERE address=? AND date=?",
                (addr, date),
            ).fetchone()
        finally:
            conn.close()
    if not row or not row["markets_json"]:
        return None
    data = _loads(row["markets_json"])
    if isinstance(data, dict):
        data = {**data, "cached": True}
    return data


def get_day_activity(address: str, date: str) -> dict[str, Any] | None:
    addr = address.lower()
    with _LOCK:
        conn = _connect()
        try:
            _init(conn)
            row = conn.execute(
                "SELECT activity_json FROM wallet_day_cache WHERE address=? AND date=?",
                (addr, date),
            ).fetchone()
        finally:
            conn.close()
    if not row or not row["activity_json"]:
        return None
    data = _loads(row["activity_json"])
    if isinstance(data, dict):
        data = {**data, "cached": True}
    return data


def db_path() -> Path:
    return _DB_PATH
