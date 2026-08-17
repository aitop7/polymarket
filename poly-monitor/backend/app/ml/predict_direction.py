"""Cached inference for the short-horizon Up/Down direction models."""

from __future__ import annotations

from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.data import DIRECTION_FEATURE_COLUMNS
from app.core.live_dataset import find_live_market_dir
from app.ml.live_features import engineer_features, load_live_feature_frame
from app.ml.train_predict_up import direction_model_filename

_MODELS: dict[float, tuple[float, lgb.Booster]] = {}


def _load_model(horizon_seconds: float) -> lgb.Booster:
    path = settings.models_dir / direction_model_filename(horizon_seconds)
    if not path.is_file():
        raise FileNotFoundError(f"Direction model not found: {path.name}")
    stamp = path.stat().st_mtime
    cached = _MODELS.get(float(horizon_seconds))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    model = lgb.Booster(model_file=str(path))
    if model.feature_name() != list(DIRECTION_FEATURE_COLUMNS):
        raise RuntimeError("Direction model feature schema does not match the running application")
    _MODELS[float(horizon_seconds)] = (stamp, model)
    return model


def _finite(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def _band_qty_map(side_rows: list[dict[str, Any]] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in side_rows or []:
        suffix = str(row.get("suffix") or "").strip()
        if not suffix:
            continue
        qty = _finite(row.get("qty"))
        if qty is None:
            continue
        out[suffix] = float(qty)
    return out


def _frame_from_live_snapshot(
    *,
    market_id: str,
    series: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> pd.DataFrame:
    """Build a causal feature frame from the live monitor buffer when parquet is missing."""
    start_ms = int(snapshot.get("start_time") or 0) or None
    end_ms = int(snapshot.get("end_time") or 0) or None
    rows: list[dict[str, Any]] = []
    for point in series:
        ts = _finite(point.get("t") if "t" in point else point.get("timestamp"))
        if ts is None:
            continue
        row: dict[str, Any] = {
            "timestamp": int(ts),
            "up_price": _finite(point.get("up") if "up" in point else point.get("up_price")),
            "down_price": _finite(point.get("down") if "down" in point else point.get("down_price")),
            "btc_price": _finite(point.get("btc") if "btc" in point else point.get("btc_price")),
            "btc_chainlink": _finite(
                point.get("chainlink") if "chainlink" in point else point.get("btc_chainlink")
            ),
            "btc_twap_30s": _finite(point.get("twap") if "twap" in point else point.get("btc_twap_30s")),
        }
        if start_ms is not None:
            row["start_time"] = start_ms
        if end_ms is not None:
            row["end_time"] = end_ms
        rows.append(row)

    if not rows:
        now_ms = int(_finite(snapshot.get("timestamp")) or pd.Timestamp.utcnow().timestamp() * 1000)
        rows.append(
            {
                "timestamp": now_ms,
                "up_price": _finite(snapshot.get("up_price")),
                "down_price": _finite(snapshot.get("down_price")),
                "btc_price": _finite(snapshot.get("btc_price")),
                "btc_chainlink": _finite(snapshot.get("btc_chainlink")),
                "btc_twap_30s": _finite(snapshot.get("btc_twap_30s")),
                "start_time": start_ms,
                "end_time": end_ms,
            }
        )

    # Attach the latest CLOB / Binance books onto the newest row so microprice / OBI work.
    last = rows[-1]
    book = snapshot.get("book") if isinstance(snapshot.get("book"), dict) else {}
    for side in ("up", "down"):
        side_book = book.get(side) if isinstance(book, dict) else None
        if not isinstance(side_book, dict):
            continue
        last[f"{side}_bid_price"] = _finite(side_book.get("best_bid"))
        last[f"{side}_ask_price"] = _finite(side_book.get("best_ask"))
        bids = side_book.get("bids") or []
        asks = side_book.get("asks") or []
        bid_shares = _finite(bids[0].get("shares")) if bids else None
        ask_shares = _finite(asks[0].get("shares")) if asks else None
        last[f"{side}_bid_shares"] = bid_shares
        last[f"{side}_ask_shares"] = ask_shares
        # Live ladder has no distance bands; use best-level size as a 0_1 proxy.
        last[f"{side}_bid_0_1"] = bid_shares
        last[f"{side}_ask_0_1"] = ask_shares

    binance_book = snapshot.get("binance_book") if isinstance(snapshot.get("binance_book"), dict) else {}
    if isinstance(binance_book, dict):
        ask_map = _band_qty_map(binance_book.get("asks"))
        bid_map = _band_qty_map(binance_book.get("bids"))
        for suffix, qty in ask_map.items():
            last[f"ask_{suffix}"] = qty
        for suffix, qty in bid_map.items():
            last[f"bid_{suffix}"] = qty
        mid = _finite(binance_book.get("mid") or binance_book.get("price") or snapshot.get("btc_price"))
        if mid is not None:
            last["btc_price"] = mid

    frame = pd.DataFrame(rows).sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    frame.attrs["market_id"] = str(market_id)
    return engineer_features(frame)


def _score_feature_row(row: pd.Series, *, horizons: tuple[float, ...]) -> dict[str, Any]:
    values = pd.to_numeric(row.reindex(DIRECTION_FEATURE_COLUMNS), errors="coerce").to_numpy(
        dtype=np.float32
    )
    feature_coverage = float(np.mean(np.isfinite(values)))
    if feature_coverage < 0.35:
        raise RuntimeError(
            f"Insufficient live feature coverage ({feature_coverage:.0%}); wait for market data to accumulate"
        )

    predictions: list[dict[str, Any]] = []
    for horizon in horizons:
        probability_up = float(_load_model(float(horizon)).predict(values.reshape(1, -1))[0])
        confidence = abs(probability_up - 0.5) * 2.0
        predictions.append(
            {
                "horizon_seconds": float(horizon),
                "probability_up": probability_up,
                "probability_down": 1.0 - probability_up,
                "direction": "UP" if probability_up >= 0.5 else "DOWN",
                "confidence": confidence,
            }
        )
    timestamp = int(pd.to_numeric(row.get("timestamp"), errors="coerce"))
    now_ms = int(pd.Timestamp.utcnow().timestamp() * 1000)
    return {
        "timestamp": timestamp,
        "age_ms": max(0, now_ms - timestamp),
        "feature_coverage": feature_coverage,
        "predictions": predictions,
    }


def predict_direction(
    market_id: str,
    *,
    horizons: tuple[float, ...] = (3.0, 5.0),
    series: list[dict[str, Any]] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score the latest live row for each requested horizon.

    Prefers persisted fetch_live parquet when present; otherwise builds features from the
    in-memory live monitor series/snapshot so in-progress markets still score.
    """
    mid = str(market_id)
    source = "parquet"
    try:
        if find_live_market_dir(mid) is not None:
            df = load_live_feature_frame(mid)
        else:
            raise FileNotFoundError(mid)
    except FileNotFoundError:
        if not snapshot:
            raise FileNotFoundError(
                f"Live market not found locally yet: {mid}. Waiting for capture/sync."
            ) from None
        df = _frame_from_live_snapshot(
            market_id=mid,
            series=series or [],
            snapshot=snapshot,
        )
        source = "live_buffer"

    if df.empty:
        raise RuntimeError("No live data for this market yet")

    scored = _score_feature_row(df.iloc[-1], horizons=horizons)
    return {
        "market_id": mid,
        "source": source,
        **scored,
    }
