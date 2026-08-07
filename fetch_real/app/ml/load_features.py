"""Load Step-2 feature parquets for modeling."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from app.dataset.feature_schema import FEATURE_COLUMNS, ID_COLUMNS

SPLITS = ("train", "validation", "test")


def list_market_files(features_root: Path, split: str) -> list[Path]:
    split_dir = Path(features_root) / split
    if not split_dir.is_dir():
        return []
    return sorted(split_dir.glob("*.parquet"))


def load_split(
    features_root: Path,
    split: str,
    *,
    columns: list[str] | None = None,
    max_markets: int | None = None,
) -> pd.DataFrame:
    """Concatenate all market feature files for a split."""
    files = list_market_files(features_root, split)
    if max_markets is not None:
        files = files[: max(0, int(max_markets))]
    if not files:
        raise FileNotFoundError(f"No feature parquets under {features_root / split}")

    cols = columns
    frames: list[pd.DataFrame] = []
    for path in files:
        frames.append(pd.read_parquet(path, columns=cols))
    return pd.concat(frames, ignore_index=True)


def feature_matrix(df: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray, pd.DataFrame]:
    """
    Returns (X, y, meta) where meta keeps id/price cols for evaluation.

    X uses FEATURE_COLUMNS only; missing cols raise.
    """
    missing = [c for c in FEATURE_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing feature columns: {missing[:10]}...")

    X = df.loc[:, FEATURE_COLUMNS].astype(np.float32)
    y = df["winner"].astype(np.int8).to_numpy()
    meta_cols = [c for c in ID_COLUMNS if c in df.columns]
    meta = df.loc[:, meta_cols].copy()
    return X, y, meta
