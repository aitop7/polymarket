"""Persist Price-To-Beat by 5m window start so reloads don't re-lock to 'now'."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_STORE_PATH = Path(__file__).resolve().parents[2] / ".cache" / "price_to_beat.json"
_MAX_AGE_MS = 36 * 3600 * 1000  # keep ~1.5 days

# Price To Beat = previous market close 30s TWAP (not live spot / not TWAP-now).
VALID_SOURCES = frozenset(
    {
        "prev_close_twap_30s",
        "prev_close_twap_30s_computed",
        # legacy aliases (same boundary timestamp)
        "open_twap_30s",
        "open_twap_30s_computed",
        "twap_30s",
        "twap_30s_computed",
    }
)


def _load() -> dict[str, Any]:
    if not _STORE_PATH.is_file():
        return {}
    try:
        return json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict[str, Any]) -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time() * 1000)
    pruned = {
        k: v
        for k, v in data.items()
        if isinstance(v, dict) and now - int(v.get("saved_at") or 0) < _MAX_AGE_MS
    }
    _STORE_PATH.write_text(json.dumps(pruned, indent=2), encoding="utf-8")


def get_price_to_beat(window_start_ms: int) -> dict[str, Any] | None:
    data = _load()
    key = str(int(window_start_ms))
    row = data.get(key)
    if not isinstance(row, dict):
        return None
    source = str(row.get("source") or "unknown")
    if source not in VALID_SOURCES:
        # Drop legacy Chainlink-spot / Binance-open locks.
        del data[key]
        _save(data)
        return None
    try:
        return {
            "price": float(row["price"]),
            "source": source,
            "observed_ts": row.get("observed_ts"),
        }
    except Exception:
        return None


def set_price_to_beat(
    window_start_ms: int,
    price: float,
    *,
    source: str,
    observed_ts: int | None = None,
    overwrite: bool = False,
) -> None:
    data = _load()
    key = str(int(window_start_ms))
    if (
        not overwrite
        and key in data
        and isinstance(data[key], dict)
        and data[key].get("price") is not None
    ):
        return  # never overwrite a locked open
    data[key] = {
        "price": float(price),
        "source": source,
        "observed_ts": observed_ts,
        "saved_at": int(time.time() * 1000),
    }
    _save(data)


def clear_price_to_beat(window_start_ms: int) -> None:
    data = _load()
    key = str(int(window_start_ms))
    if key in data:
        del data[key]
        _save(data)
