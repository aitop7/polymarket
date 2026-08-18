"""Univariate feature relevance vs Up-mid delta (future − now).

Loads fetch_real/features_live/*.parquet, builds label
  delta_up = up_mid[t + h] − up_mid[t]
and scores each feature in [0, 1] as a coverage-weighted mix of
|Spearman| and |Pearson|.

Usage (from poly-monitor/backend):

  python -m app.ml.feature_relevance --horizon 5
  python -m app.ml.feature_relevance --horizons 3,5 --max-markets 500
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.data import DIRECTION_FEATURE_COLUMNS, FEATURE_COLUMNS

# Columns that define the label path — still scored, but flagged as leakage-ish.
LEAKAGE_HINTS = frozenset({"up_mid", "down_mid", "up_price", "down_price"})


def _feature_list(manifest: dict[str, Any] | None) -> list[str]:
    if manifest:
        cols = list(manifest.get("feature_columns") or [])
        cols += [c for c in (manifest.get("direction_feature_columns") or []) if c not in cols]
        if cols:
            return cols
    return list(dict.fromkeys([*FEATURE_COLUMNS, *DIRECTION_FEATURE_COLUMNS]))


def _iter_parquets(root: Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    paths = sorted(root.glob("*.parquet"))
    if paths:
        return paths
    out: list[Path] = []
    for split in ("train", "validation", "test"):
        d = root / split
        if d.is_dir():
            out.extend(sorted(d.glob("*.parquet")))
    return out


def load_feature_frames(
    root: Path,
    *,
    max_markets: int | None = None,
) -> pd.DataFrame:
    paths = _iter_parquets(root)
    if max_markets is not None and max_markets > 0:
        paths = paths[-int(max_markets) :]
    if not paths:
        raise SystemExit(f"No feature parquet under {root}")

    frames: list[pd.DataFrame] = []
    for path in paths:
        try:
            df = pd.read_parquet(path)
        except Exception as exc:
            print(f"skip {path.name}: {exc}", flush=True)
            continue
        if df.empty or "timestamp" not in df.columns:
            continue
        if "market_id" not in df.columns:
            df = df.copy()
            df["market_id"] = path.stem
        frames.append(df)
    if not frames:
        raise SystemExit(f"No readable feature frames under {root}")
    return pd.concat(frames, ignore_index=True, sort=False)


def _build_delta_label(
    df: pd.DataFrame,
    *,
    horizon_seconds: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-market causal forward delta on up_mid; returns (delta, valid_mask)."""
    if "up_mid" in df.columns:
        mid = pd.to_numeric(df["up_mid"], errors="coerce").to_numpy(dtype=np.float64)
    elif "up_price" in df.columns:
        mid = pd.to_numeric(df["up_price"], errors="coerce").to_numpy(dtype=np.float64)
    else:
        raise SystemExit("Feature frames missing up_mid / up_price")

    ts = pd.to_numeric(df["timestamp"], errors="coerce").to_numpy(dtype=np.int64)
    mids = df["market_id"].astype(str).to_numpy() if "market_id" in df.columns else np.array(["_"] * len(df))
    end = (
        pd.to_numeric(df["end_time"], errors="coerce").to_numpy(dtype=np.float64)
        if "end_time" in df.columns
        else np.full(len(df), np.nan)
    )

    horizon_ms = int(round(float(horizon_seconds) * 1000))
    delta = np.full(len(df), np.nan, dtype=np.float64)

    work = pd.DataFrame({"ts": ts, "mid": mid, "end": end, "market_id": mids})
    for _, g in work.groupby("market_id", sort=False):
        idx = g.index.to_numpy()
        gts = g["ts"].to_numpy(dtype=np.int64)
        gmid = g["mid"].to_numpy(dtype=np.float64)
        order_i = np.argsort(gts, kind="mergesort")
        idx = idx[order_i]
        gts = gts[order_i]
        gmid = gmid[order_i]
        fut = np.searchsorted(gts, gts + horizon_ms, side="left")
        valid = fut < len(gts)
        d = np.full(len(gts), np.nan, dtype=np.float64)
        d[valid] = gmid[fut[valid]] - gmid[valid]
        # Purge labels that would cross market end.
        gend = g["end"].to_numpy(dtype=np.float64)[order_i]
        if np.isfinite(gend).any():
            end0 = float(gend[np.isfinite(gend)][0])
            d = np.where(gts + horizon_ms <= end0, d, np.nan)
        delta[idx] = d

    mask = np.isfinite(delta) & np.isfinite(mid)
    return delta, mask


def _safe_corr(x: np.ndarray, y: np.ndarray, *, method: str) -> float:
    if len(x) < 30:
        return float("nan")
    s = pd.Series(x)
    t = pd.Series(y)
    try:
        r = s.corr(t, method=method)
    except Exception:
        return float("nan")
    if r is None or not np.isfinite(r):
        return float("nan")
    return float(r)


def score_features(
    df: pd.DataFrame,
    *,
    horizon_seconds: float,
    feature_columns: list[str],
    spearman_weight: float = 0.65,
) -> dict[str, Any]:
    delta, base_mask = _build_delta_label(df, horizon_seconds=horizon_seconds)
    y = delta[base_mask]
    n_labeled = int(base_mask.sum())

    rows: list[dict[str, Any]] = []
    w_s = float(spearman_weight)
    w_p = 1.0 - w_s

    for col in feature_columns:
        if col not in df.columns:
            rows.append(
                {
                    "feature": col,
                    "relevance": 0.0,
                    "abs_spearman": None,
                    "abs_pearson": None,
                    "spearman": None,
                    "pearson": None,
                    "coverage": 0.0,
                    "n": 0,
                    "leakage_hint": col in LEAKAGE_HINTS,
                    "missing": True,
                }
            )
            continue

        x_all = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=np.float64)
        ok = base_mask & np.isfinite(x_all)
        n = int(ok.sum())
        coverage = float(n / max(1, n_labeled))
        if n < 30:
            rows.append(
                {
                    "feature": col,
                    "relevance": 0.0,
                    "abs_spearman": None,
                    "abs_pearson": None,
                    "spearman": None,
                    "pearson": None,
                    "coverage": round(coverage, 4),
                    "n": n,
                    "leakage_hint": col in LEAKAGE_HINTS,
                    "missing": False,
                }
            )
            continue

        x = x_all[ok]
        yy = delta[ok]
        # Drop near-constant features.
        if float(np.nanstd(x)) < 1e-12:
            rows.append(
                {
                    "feature": col,
                    "relevance": 0.0,
                    "abs_spearman": 0.0,
                    "abs_pearson": 0.0,
                    "spearman": 0.0,
                    "pearson": 0.0,
                    "coverage": round(coverage, 4),
                    "n": n,
                    "leakage_hint": col in LEAKAGE_HINTS,
                    "constant": True,
                }
            )
            continue

        sp = _safe_corr(x, yy, method="spearman")
        pe = _safe_corr(x, yy, method="pearson")
        abs_sp = abs(sp) if np.isfinite(sp) else 0.0
        abs_pe = abs(pe) if np.isfinite(pe) else 0.0
        # Relevance in [0, 1]: correlation strength × coverage (sparse feats down-weighted).
        raw = w_s * abs_sp + w_p * abs_pe
        relevance = float(np.clip(raw * (0.5 + 0.5 * coverage), 0.0, 1.0))

        rows.append(
            {
                "feature": col,
                "relevance": round(relevance, 4),
                "abs_spearman": round(abs_sp, 4),
                "abs_pearson": round(abs_pe, 4),
                "spearman": round(sp, 4) if np.isfinite(sp) else None,
                "pearson": round(pe, 4) if np.isfinite(pe) else None,
                "coverage": round(coverage, 4),
                "n": n,
                "leakage_hint": col in LEAKAGE_HINTS,
            }
        )

    rows.sort(key=lambda r: (-float(r["relevance"]), str(r["feature"])))
    return {
        "horizon_seconds": float(horizon_seconds),
        "label": "delta_up_mid = up_mid[t+h] - up_mid[t]",
        "relevance_formula": (
            "clip((0.65*|spearman| + 0.35*|pearson|) * (0.5 + 0.5*coverage), 0, 1)"
        ),
        "n_rows": int(len(df)),
        "n_labeled": n_labeled,
        "n_markets": int(df["market_id"].nunique()) if "market_id" in df.columns else None,
        "delta_mean": float(np.nanmean(y)) if len(y) else None,
        "delta_std": float(np.nanstd(y)) if len(y) else None,
        "features": rows,
    }


def analyze(
    *,
    features_dir: Path | None = None,
    horizons: list[float],
    max_markets: int | None = None,
) -> dict[str, Any]:
    root = Path(features_dir) if features_dir else settings.features_live_dir
    manifest_path = root / "manifest.json"
    manifest = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = None

    feature_columns = _feature_list(manifest)
    df = load_feature_frames(root, max_markets=max_markets)
    by_horizon = {
        str(h): score_features(df, horizon_seconds=h, feature_columns=feature_columns)
        for h in horizons
    }

    report = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "features_dir": str(root.resolve()),
        "feature_columns": feature_columns,
        "horizons": horizons,
        "by_horizon": by_horizon,
    }
    out_path = root / "feature_relevance.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}", flush=True)
    return report


def _print_top(report: dict[str, Any], *, top: int = 25) -> None:
    for h, block in report["by_horizon"].items():
        print(f"\n=== horizon {h}s · n_labeled={block['n_labeled']} · markets={block['n_markets']} ===")
        print(f"{'relevance':>10} {'|sp|':>7} {'|pe|':>7} {'cov':>6}  feature")
        for row in block["features"][:top]:
            print(
                f"{row['relevance']:10.4f} "
                f"{(row['abs_spearman'] or 0):7.4f} "
                f"{(row['abs_pearson'] or 0):7.4f} "
                f"{row['coverage']:6.3f}  {row['feature']}"
                f"{'  [level]' if row.get('leakage_hint') else ''}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score feature relevance vs delta up_mid")
    parser.add_argument("--features-dir", type=Path, default=None)
    parser.add_argument("--horizon", type=float, default=None, help="Single horizon seconds")
    parser.add_argument("--horizons", type=str, default="3,5", help="Comma list, default 3,5")
    parser.add_argument("--max-markets", type=int, default=None)
    parser.add_argument("--top", type=int, default=30)
    args = parser.parse_args(argv)

    if args.horizon is not None:
        horizons = [float(args.horizon)]
    else:
        horizons = [
            float(x.strip())
            for x in str(args.horizons).split(",")
            if x.strip() and float(x.strip()) > 0
        ]
    if not horizons:
        raise SystemExit("No horizons")

    report = analyze(
        features_dir=args.features_dir,
        horizons=horizons,
        max_markets=args.max_markets,
    )
    _print_top(report, top=args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
