from app.config import settings


def whale_score(size: float, price: float | None = None, threshold: float | None = None) -> float:
    thr = threshold if threshold is not None else settings.whale_trade_threshold
    notional = size * (price if price is not None else 1.0)
    if thr <= 0:
        return 0.0
    return max(0.0, notional / thr)


def is_whale(size: float, price: float | None = None, threshold: float | None = None) -> bool:
    return whale_score(size, price, threshold) >= 1.0
