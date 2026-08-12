"""Polymarket CLOB taker fee helpers.

Official taker fee (docs.polymarket.com/trading/fees):
    fee_usdc = C × feeRate × p × (1 − p)

BTC 5-minute Up/Down markets use the Crypto category (feeRate ≈ 0.07).
Makers pay zero; settlement/redemption is free.
"""

from __future__ import annotations

from typing import Literal

FeeModel = Literal["none", "polymarket", "flat"]

# Polymarket crypto category default (2026 docs).
CRYPTO_TAKER_FEE_RATE = 0.07


def polymarket_taker_fee_usdc(shares: float, price: float, fee_rate: float) -> float:
    """USDC taker fee for one leg: C × feeRate × p × (1 − p)."""
    if fee_rate <= 0 or shares <= 0:
        return 0.0
    p = min(0.999, max(1e-6, float(price)))
    return float(shares) * float(fee_rate) * p * (1.0 - p)


def per_share_fee_drag(price: float, fee_rate: float, fee_model: str) -> float:
    """Estimated fee cost per share (for edge checks and sizing)."""
    if fee_model == "none" or fee_rate <= 0:
        return 0.0
    p = min(0.999, max(1e-6, float(price)))
    if fee_model == "polymarket":
        return float(fee_rate) * p * (1.0 - p)
    if fee_model == "flat":
        return float(fee_rate) * p
    return 0.0


def buy_cash_required(
    shares: float,
    price: float,
    *,
    fee_rate: float,
    fee_model: str,
) -> float:
    """Total cash to buy `shares` at `price` including taker fee."""
    notional = float(shares) * float(price)
    if fee_model == "none" or fee_rate <= 0:
        return notional
    if fee_model == "polymarket":
        return notional + polymarket_taker_fee_usdc(shares, price, fee_rate)
    if fee_model == "flat":
        return notional * (1.0 + float(fee_rate))
    return notional


def pair_cash_per_share(
    up_price: float,
    down_price: float,
    *,
    fee_rate: float,
    fee_model: str,
) -> float:
    """Cash required per matched UP+DOWN share pair (both legs + fees)."""
    return buy_cash_required(1.0, up_price, fee_rate=fee_rate, fee_model=fee_model) + buy_cash_required(
        1.0, down_price, fee_rate=fee_rate, fee_model=fee_model
    )


def max_shares_for_cash(
    cash: float,
    up_price: float,
    down_price: float,
    *,
    fee_rate: float,
    fee_model: str,
) -> float:
    """Max equal UP/DOWN shares affordable for a pair trade."""
    per = pair_cash_per_share(up_price, down_price, fee_rate=fee_rate, fee_model=fee_model)
    if per <= 0:
        return 0.0
    return max(0.0, float(cash) / per)
