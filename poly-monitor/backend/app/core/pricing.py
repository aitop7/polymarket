"""Outcome quote rules: load Up buy only; derive Down buy and sell prices."""

from __future__ import annotations

from typing import Any

import pandas as pd


def _as_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        if isinstance(v, float) and pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def quotes_from_up_buy(up_buy: float | None) -> dict[str, float]:
    """
    Pricing model — keep exact Up buy float from storage:
      - up_buy   = DB `up_price` (not ask; ask is 1¢-grid)
      - down_buy = 1.01 − up_buy   (101¢ − up)
      - sell     = buy − 0.01      (1¢ lower)
    """
    raw = 0.5 if up_buy is None else float(up_buy)
    up = min(1.0, max(1e-6, raw))
    down = min(1.0 - 1e-6, max(1e-6, 1.01 - up))
    up_sell = max(1e-6, up - 0.01)
    down_sell = max(1e-6, down - 0.01)
    return {
        "up_buy": up,
        "down_buy": down,
        "up_sell": up_sell,
        "down_sell": down_sell,
        "up_price": up,
        "down_price": down,
    }


def quotes_from_row(row: Any) -> dict[str, float]:
    """Use exact `up_price` only (ask is cent-rounded and hides sub-cent)."""
    up = None
    try:
        if isinstance(row, pd.Series):
            if "up_price" in row.index:
                up = _as_float(row["up_price"])
        elif isinstance(row, dict):
            up = _as_float(row.get("up_price"))
    except Exception:
        up = None
    return quotes_from_up_buy(up)
