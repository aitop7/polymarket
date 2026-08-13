"""Background LightGBM training jobs (subprocess into fetch_real)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import settings

_lock = threading.Lock()
_job: dict[str, Any] = {
    "status": "idle",  # idle | running | succeeded | failed
    "started_at": None,
    "finished_at": None,
    "error": None,
    "log_tail": [],
    "metrics": None,
    "params": None,
    "pid": None,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_train_status() -> dict[str, Any]:
    with _lock:
        return dict(_job)


def get_model_info() -> dict[str, Any]:
    models = settings.models_dir
    features = settings.features_dir
    model_path = models / "lgbm_baseline.txt"
    metrics_path = models / "metrics.json"
    feature_names_path = models / "feature_names.json"

    splits: dict[str, Any] = {}
    for split in ("train", "validation", "test"):
        d = features / split
        n = len(list(d.glob("*.parquet"))) if d.is_dir() else 0
        splits[split] = {"path": str(d), "exists": d.is_dir(), "n_markets": n}

    metrics = None
    if metrics_path.is_file():
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except Exception as exc:
            metrics = {"error": f"failed to read metrics.json: {exc}"}

    feature_names = None
    if feature_names_path.is_file():
        try:
            feature_names = json.loads(feature_names_path.read_text(encoding="utf-8"))
        except Exception:
            feature_names = None

    try:
        from strategies.lgbm_edge import FEATURE_COLUMNS

        schema_features = list(FEATURE_COLUMNS)
    except Exception:
        schema_features = []

    return {
        "models_dir": str(models),
        "features_dir": str(features),
        "model_path": str(model_path),
        "model_exists": model_path.is_file(),
        "model_mtime": (
            datetime.fromtimestamp(model_path.stat().st_mtime, tz=timezone.utc).isoformat()
            if model_path.is_file()
            else None
        ),
        "metrics_path": str(metrics_path),
        "metrics": metrics,
        "feature_names": feature_names,
        "schema_features": schema_features,
        "n_schema_features": len(schema_features),
        "splits": splits,
        "train_job": get_train_status(),
    }


def start_lgbm_train(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Spawn fetch_real LightGBM trainer. Returns current job snapshot."""
    params = dict(params or {})
    features = settings.features_dir
    models = settings.models_dir
    fetch_root = settings.fetch_real_root

    if not features.is_dir():
        return {
            "ok": False,
            "error": f"Features directory missing: {features}",
            "job": get_train_status(),
        }
    for split in ("train", "validation"):
        if not (features / split).is_dir():
            return {
                "ok": False,
                "error": f"Missing features split: {features / split}",
                "job": get_train_status(),
            }

    num_boost_round = int(params.get("num_boost_round", 500))
    early_stopping = int(params.get("early_stopping_rounds", params.get("early_stopping", 50)))
    max_markets = params.get("max_markets")
    max_markets_i = int(max_markets) if max_markets not in (None, "", "null") else None

    cmd = [
        sys.executable,
        "-m",
        "app.ml.train_lgbm",
        "--features",
        str(features),
        "--models-dir",
        str(models),
        "--num-boost-round",
        str(num_boost_round),
        "--early-stopping",
        str(early_stopping),
    ]
    if max_markets_i is not None:
        cmd.extend(["--max-markets", str(max_markets_i)])

    env = os.environ.copy()
    # Prefer fetch_real's `app` package for the trainer module.
    env["PYTHONPATH"] = str(fetch_root) + os.pathsep + env.get("PYTHONPATH", "")
    log_file = models / "train_last.log"

    with _lock:
        if _job.get("status") == "running":
            return {"ok": False, "error": "Training already running", "job": dict(_job)}
        models.mkdir(parents=True, exist_ok=True)
        _job.update(
            {
                "status": "running",
                "started_at": _now_iso(),
                "finished_at": None,
                "error": None,
                "log_tail": [],
                "metrics": None,
                "params": {
                    "num_boost_round": num_boost_round,
                    "early_stopping_rounds": early_stopping,
                    "max_markets": max_markets_i,
                    "cmd": cmd,
                },
                "pid": None,
                "log_path": str(log_file),
            }
        )

    def _runner() -> None:
        try:
            with log_file.open("w", encoding="utf-8") as logf:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(fetch_root),
                    env=env,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            with _lock:
                _job["pid"] = proc.pid
            code = proc.wait()
            tail = _read_tail(log_file, max_lines=80)
            metrics = None
            metrics_path = models / "metrics.json"
            if metrics_path.is_file():
                try:
                    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
                except Exception:
                    metrics = None
            with _lock:
                _job["finished_at"] = _now_iso()
                _job["log_tail"] = tail
                _job["metrics"] = metrics
                if code == 0:
                    _job["status"] = "succeeded"
                    _job["error"] = None
                    try:
                        from app.core.strategy_versions import save_train_result

                        snap = save_train_result(
                            train_params=dict((_job.get("params") or {})),
                            metrics=metrics if isinstance(metrics, dict) else None,
                            label="auto-train",
                        )
                        _job["version"] = {
                            "id": snap.get("id"),
                            "path": snap.get("path"),
                        }
                    except Exception as ver_exc:
                        _job["version_error"] = str(ver_exc)
                else:
                    _job["status"] = "failed"
                    _job["error"] = f"Trainer exited with code {code}"
        except Exception as exc:
            with _lock:
                _job["status"] = "failed"
                _job["finished_at"] = _now_iso()
                _job["error"] = str(exc)
                _job["log_tail"] = _read_tail(log_file, max_lines=80)

    threading.Thread(target=_runner, daemon=True, name="lgbm-train").start()
    time.sleep(0.05)
    with _lock:
        return {"ok": True, "job": dict(_job)}


def _read_tail(path: Path, *, max_lines: int = 80) -> list[str]:
    try:
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-max_lines:]
    except Exception:
        return []
