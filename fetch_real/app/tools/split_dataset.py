"""Chronologically split data/data into train / validation / test (70:15:15)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

# fetch_real/app/tools/this_file -> parents[2] == fetch_real
ROOT = Path(__file__).resolve().parents[2] / "data" / "data"
RATIOS = (("train", 0.70), ("validation", 0.15), ("test", 0.15))
SEED_NOTE = "chronological by UTC date (no shuffle; avoids future leakage)"


def main() -> None:
    if not ROOT.is_dir():
        raise SystemExit(f"Missing data root: {ROOT}")

    dates = sorted(p for p in ROOT.iterdir() if p.is_dir() and p.name[:4].isdigit())
    if not dates:
        raise SystemExit(f"No date folders under {ROOT}")

    # Already split?
    if any((ROOT / name).exists() for name, _ in RATIOS):
        raise SystemExit(
            f"Split folders already exist under {ROOT}. "
            "Remove train/validation/test first if you want to re-run."
        )

    n = len(dates)
    n_train = int(n * 0.70)
    n_val = int(n * 0.15)
    # remainder goes to test so totals match
    n_test = n - n_train - n_val
    buckets = {
        "train": dates[:n_train],
        "validation": dates[n_train : n_train + n_val],
        "test": dates[n_train + n_val :],
    }

    manifest: dict = {
        "source": str(ROOT),
        "method": SEED_NOTE,
        "ratios": {"train": 0.70, "validation": 0.15, "test": 0.15},
        "n_dates_total": n,
        "splits": {},
    }

    for split, day_dirs in buckets.items():
        dest_root = ROOT / split
        dest_root.mkdir(parents=True, exist_ok=False)
        market_count = 0
        for day in day_dirs:
            market_count += sum(1 for p in day.iterdir() if p.is_dir())
            target = dest_root / day.name
            print(f"move {day.name} -> {split}/")
            shutil.move(str(day), str(target))
        manifest["splits"][split] = {
            "n_dates": len(day_dirs),
            "n_markets": market_count,
            "date_start": day_dirs[0].name if day_dirs else None,
            "date_end": day_dirs[-1].name if day_dirs else None,
            "dates": [d.name for d in day_dirs],
        }

    out = ROOT / "split_manifest.json"
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("---")
    for split, info in manifest["splits"].items():
        print(
            f"{split}: {info['n_dates']} days, {info['n_markets']} markets "
            f"({info['date_start']} .. {info['date_end']})"
        )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
