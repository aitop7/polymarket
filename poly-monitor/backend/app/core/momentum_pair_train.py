"""Background training job for momentum_pair UP-mid predictor (with progress %)."""

from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_job: dict[str, Any] = {
    "status": "idle",
    "progress": 0,
    "phase": None,
    "message": None,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "params": None,
    "metrics": None,
    "version": None,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_train_status() -> dict[str, Any]:
    with _lock:
        return dict(_job)


def start_train(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(params or {})
    with _lock:
        if _job.get("status") == "running":
            return {"ok": False, "error": "Training already running", "job": dict(_job)}
        _job.update(
            {
                "status": "running",
                "progress": 0,
                "phase": "split",
                "message": "Starting…",
                "started_at": _now_iso(),
                "finished_at": None,
                "error": None,
                "params": params,
                "metrics": None,
                "version": None,
                "version_error": None,
            }
        )

    def _runner() -> None:
        try:
            from app.ml.train_up_price import train_up_price_model

            def on_progress(payload: dict[str, Any]) -> None:
                with _lock:
                    if _job.get("status") != "running":
                        return
                    if "progress" in payload:
                        _job["progress"] = int(payload["progress"])
                    if "phase" in payload:
                        _job["phase"] = payload["phase"]
                    if "message" in payload:
                        _job["message"] = payload["message"]

            horizon = float(params.get("horizon_seconds", params.get("T", 5.0)))
            result = train_up_price_model(
                horizon_seconds=horizon,
                train_ratio=float(params.get("train_ratio", 0.8)),
                num_boost_round=int(params.get("num_boost_round", 400)),
                early_stopping_rounds=int(params.get("early_stopping_rounds", 40)),
                max_markets=(
                    int(params["max_markets"])
                    if params.get("max_markets") not in (None, "", "null")
                    else None
                ),
                progress_cb=on_progress,
            )
            metrics = result.get("metrics") or {}
            version: dict[str, Any] | None = None
            try:
                from app.core.strategy_versions import save_version, strategy_dir

                src = Path(str(result["model_path"]))
                version = save_version(
                    "momentum_pair",
                    runtime_params={
                        "size_usd": 10.0,
                        "horizon_seconds": horizon,
                        "delta_seconds": float(params.get("delta_seconds", 1.0)),
                        "min_fail_drop": 0.02,
                        "min_pair_edge": 0.0,
                        "model_path": str(src),
                    },
                    train_params={
                        "horizon_seconds": horizon,
                        "train_ratio": float(params.get("train_ratio", 0.8)),
                        "num_boost_round": int(params.get("num_boost_round", 400)),
                        "early_stopping_rounds": int(
                            params.get("early_stopping_rounds", 40)
                        ),
                        "max_markets": params.get("max_markets"),
                    },
                    label="auto-train",
                    kind="train",
                    metrics=metrics if isinstance(metrics, dict) else None,
                    copy_lgbm_artifacts=False,
                    make_active=True,
                )
                vid = str(version.get("id"))
                d = strategy_dir("momentum_pair")
                dest = d / f"{vid}.model.txt"
                if src.is_file():
                    shutil.copy2(src, dest)
                man_path = d / f"{vid}.json"
                if man_path.is_file():
                    man = json.loads(man_path.read_text(encoding="utf-8"))
                    arts = dict(man.get("artifacts") or {})
                    arts["model"] = dest.name
                    man["artifacts"] = arts
                    man["runtime_params"] = {
                        **dict(man.get("runtime_params") or {}),
                        "model_path": str(dest),
                    }
                    man_path.write_text(json.dumps(man, indent=2), encoding="utf-8")
                    version = man
            except Exception as ver_exc:
                with _lock:
                    _job["version_error"] = str(ver_exc)

            with _lock:
                _job["status"] = "succeeded"
                _job["progress"] = 100
                _job["phase"] = "save"
                _job["message"] = "Done"
                _job["finished_at"] = _now_iso()
                _job["metrics"] = metrics
                if version is not None:
                    _job["version"] = {
                        "id": version.get("id"),
                        "path": version.get("path"),
                    }
        except Exception as exc:
            with _lock:
                _job["status"] = "failed"
                _job["finished_at"] = _now_iso()
                _job["error"] = str(exc)
                _job["message"] = str(exc)

    threading.Thread(target=_runner, daemon=True, name="momentum-pair-train").start()
    time.sleep(0.05)
    with _lock:
        return {"ok": True, "job": dict(_job)}
