"""Compact meta.json schema for each market directory."""

from __future__ import annotations

from typing import Any

from app.features.depth_bands import timestamp_to_ms

META_KEYS = (
    "market_id",
    "start_time",
    "end_time",
    "resolved_at",
    "btc_open_price",
    "btc_close_price",
    "winner",
)


def encode_winner(value: Any) -> bool | None:
    """True=UP, False=DOWN, None=unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    text = str(value).strip().lower()
    if text in {"", "none", "null", "nan"}:
        return None
    if text in {"up", "yes", "y", "1", "true"}:
        return True
    if text in {"down", "no", "n", "0", "false"}:
        return False
    return None


def _ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        return v if v > 10_000_000_000 else v * 1000
    try:
        return timestamp_to_ms(value)
    except Exception:
        return None


def _f32(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_meta_document(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Build storage-optimized meta.json:

      market_id, start_time, end_time, resolved_at (int64 ms),
      btc_open_price, btc_close_price (float),
      winner (bool: 0=DOWN, 1=UP)
    """
    resolved = (
        payload.get("resolved_at")
        if payload.get("resolved_at") is not None
        else payload.get("settlement_time") or payload.get("closed_time")
    )
    open_px = (
        payload.get("btc_open_price")
        if payload.get("btc_open_price") is not None
        else payload.get("opening_btc_price")
    )
    close_px = (
        payload.get("btc_close_price")
        if payload.get("btc_close_price") is not None
        else payload.get("closing_btc_price")
    )
    return {
        "market_id": str(payload.get("market_id") or ""),
        "start_time": _ms(payload.get("start_time")),
        "end_time": _ms(payload.get("end_time")),
        "resolved_at": _ms(resolved),
        "btc_open_price": _f32(open_px),
        "btc_close_price": _f32(close_px),
        "winner": encode_winner(payload.get("winner")),
    }
