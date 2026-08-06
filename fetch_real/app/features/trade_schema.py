"""Flat, storage-optimized trade rows for trades.parquet."""

from __future__ import annotations

from typing import Any

from app.features.depth_bands import timestamp_to_ms

TRADE_COLUMNS = [
    "timestamp",
    "wallet",
    "token",
    "side",
    "price",
    "shares",
]


def encode_token(
    *,
    outcome: Any = None,
    asset: Any = None,
    token_yes: Any = None,
    token_no: Any = None,
    token: Any = None,
) -> bool | None:
    """True=UP, False=DOWN. None if unknown."""
    if isinstance(token, bool):
        return token
    if token is not None and not isinstance(token, str):
        try:
            return bool(int(token))
        except (TypeError, ValueError):
            pass
    oc = str(outcome or "").strip().lower()
    if oc in {"yes", "up", "y", "1", "true"}:
        return True
    if oc in {"no", "down", "n", "0", "false"}:
        return False
    if token_yes and asset is not None and str(asset) == str(token_yes):
        return True
    if token_no and asset is not None and str(asset) == str(token_no):
        return False
    return None


def encode_side(side: Any) -> bool:
    """True=BUY, False=SELL."""
    if isinstance(side, bool):
        return side
    if isinstance(side, (int, float)):
        return bool(int(side))
    s = str(side or "").strip().lower()
    if s in {"buy", "b", "1", "true"}:
        return True
    return False


def _shares_u32(size: float | None) -> int:
    if size is None:
        return 0
    v = int(round(float(size)))
    return max(0, min(v, 2**32 - 1))


def build_trade_row(
    *,
    timestamp: Any,
    wallet: Any,
    price: float,
    shares: float | None = None,
    size: float | None = None,
    side: Any = None,
    outcome: Any = None,
    asset: Any = None,
    token_yes: Any = None,
    token_no: Any = None,
    token: Any = None,
) -> dict[str, Any] | None:
    """One flat trade row. Returns None if token cannot be resolved."""
    tok = encode_token(
        outcome=outcome,
        asset=asset,
        token_yes=token_yes,
        token_no=token_no,
        token=token,
    )
    if tok is None:
        return None
    qty = shares if shares is not None else size
    return {
        "timestamp": timestamp_to_ms(timestamp),
        "wallet": "" if wallet is None else str(wallet),
        "token": bool(tok),
        "side": encode_side(side),
        "price": float(price),
        "shares": _shares_u32(qty),
    }
