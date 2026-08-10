"""Persist Price-To-Beat by 5m window start so reloads don't re-lock to 'now'."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_STORE_PATH = Path(__file__).resolve().parents[2] / ".cache" / "price_to_beat.json"
_MAX_AGE_MS = 36 * 3600 * 1000  # keep ~1.5 days

# Price To Beat = Chainlink 30s TWAP at window start, set when the market opens.
VALID_SOURCES = frozenset(
    {
        "open_twap_30s",
        "open_twap_30s_computed",
        "fetch_live_meta",
        # legacy aliases (same T0 boundary)
        "prev_close_twap_30s",
        "prev_close_twap_30s_computed",
        "twap_30s",
        "twap_30s_computed",
    }
)

# Sample farther than this from start_time is provisional (early lock).
GOOD_SAMPLE_MAX_DELTA_MS = 1_500

# Authoritative open lock = Polymarket RTDS Chainlink 30s TWAP.
# Binance-computed / meta are provisional and must never block RTDS.
PTB_SOURCE_RANK: dict[str, int] = {
    "open_twap_30s": 40,
    "prev_close_twap_30s": 40,
    "twap_30s": 35,
    "open_twap_30s_computed": 10,
    "prev_close_twap_30s_computed": 10,
    "twap_30s_computed": 10,
    "fetch_live_meta": 5,
}


def source_rank(source: str | None) -> int:
    return int(PTB_SOURCE_RANK.get(str(source or ""), 0))


def is_provisional_source(source: str | None) -> bool:
    """True when lock is not Polymarket RTDS Chainlink TWAP."""
    return source_rank(source) < PTB_SOURCE_RANK["open_twap_30s"]


def is_rtds_source(source: str | None) -> bool:
    return source_rank(source) >= PTB_SOURCE_RANK["open_twap_30s"]


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


def sample_delta_ms(window_start_ms: int, observed_ts: int | None) -> int | None:
    if observed_ts is None:
        return None
    try:
        return abs(int(observed_ts) - int(window_start_ms))
    except (TypeError, ValueError):
        return None


def is_good_sample(window_start_ms: int, observed_ts: int | None) -> bool:
    delta = sample_delta_ms(window_start_ms, observed_ts)
    return delta is not None and delta <= GOOD_SAMPLE_MAX_DELTA_MS


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
) -> bool:
    """
    Persist PTB. Returns True if written.

    Priority:
      1) overwrite=True
      2) higher-ranked source (RTDS Chainlink beats Binance-computed / meta)
      3) same rank → closer observed_ts to window start wins
    """
    data = _load()
    key = str(int(window_start_ms))
    start = int(window_start_ms)
    existing = data.get(key) if isinstance(data.get(key), dict) else None

    if existing is not None and existing.get("price") is not None and not overwrite:
        existing_source = str(existing.get("source") or "")
        new_rank = source_rank(source)
        old_rank = source_rank(existing_source)
        old_ts = existing.get("observed_ts")
        old_delta = sample_delta_ms(start, old_ts if old_ts is not None else None)
        new_delta = sample_delta_ms(start, observed_ts)

        if new_rank < old_rank:
            return False
        if new_rank == old_rank:
            # Same quality — keep the sample closer to T0.
            if old_delta is not None and (new_delta is None or new_delta >= old_delta):
                return False
        # new_rank > old_rank → always allow (RTDS replaces computed/meta)

    data[key] = {
        "price": float(price),
        "source": source,
        "observed_ts": observed_ts,
        "saved_at": int(time.time() * 1000),
    }
    _save(data)
    return True


def clear_price_to_beat(window_start_ms: int) -> None:
    data = _load()
    key = str(int(window_start_ms))
    if key in data:
        del data[key]
        _save(data)
