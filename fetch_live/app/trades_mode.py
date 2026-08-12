"""Shared collector trade-capture mode: taker-only vs full (taker + maker).

Env default: FETCH_LIVE_TRADES_MODE=full|taker
Runtime override (no restart): {data_dir}/collector_settings.json
  written by PUT /settings on the serve API; read by the collector on each fetch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from app.config import settings

TradesMode = Literal["taker", "full"]

SETTINGS_FILE = "collector_settings.json"
_TAKER_ALIASES = frozenset({"taker", "taker_only", "takers"})
_FULL_ALIASES = frozenset({"full", "all", "maker", "makers", "taker_maker", "takers_makers"})


def parse_trades_mode(value: Any) -> TradesMode | None:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in _TAKER_ALIASES:
        return "taker"
    if text in _FULL_ALIASES:
        return "full"
    return None


def normalize_trades_mode(value: Any) -> TradesMode:
    return parse_trades_mode(value) or "full"


def settings_path() -> Path:
    return Path(settings.data_dir) / SETTINGS_FILE


def _read_file_mode() -> TradesMode | None:
    path = settings_path()
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return parse_trades_mode(raw.get("trades_mode"))


def get_trades_mode() -> TradesMode:
    """File override if present, else FETCH_LIVE_TRADES_MODE (default full)."""
    file_mode = _read_file_mode()
    if file_mode is not None:
        return file_mode
    return normalize_trades_mode(settings.trades_mode)


def set_trades_mode(value: Any) -> TradesMode:
    """Persist runtime override. Collector picks it up on the next fetch/flush."""
    mode = parse_trades_mode(value)
    if mode is None:
        raise ValueError("trades_mode must be 'taker' or 'full'")
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    data["trades_mode"] = mode
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return mode


def trades_mode_source() -> str:
    return "file" if _read_file_mode() is not None else "env"
