"""Small in-process controller for prediction-model inventory and training jobs."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.ml.train_predict_up import (
    direction_eval_filename,
    direction_metrics_filename,
    direction_model_filename,
    evaluate_predict_up_direction,
    train_predict_up_direction_horizon,
)
from app.ml.train_predict_up_beta import (
    beta_eval_filename,
    beta_logvar_filename,
    beta_mean_filename,
    beta_metrics_filename,
    evaluate_beta_horizon,
    train_beta_horizon,
)
from app.ml.train_predict_up_beta_ct import (
    continuous_model_ready,
    evaluate_beta_continuous,
    train_beta_continuous,
)

_LOCK = threading.Lock()
_ACTIVE_FILE = settings.models_dir / "direction_active_horizons.json"
_JOB: dict[str, Any] | None = None


def _horizon_tag(value: float) -> str:
    return f"{float(value):g}"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def available_models(*, kind: str = "direction") -> list[dict[str, Any]]:
    """Return a model family's artifacts plus its saved held-out metrics."""
    if kind == "beta":
        rows: list[dict[str, Any]] = []
        for path in sorted(settings.models_dir.glob("predict_up_beta_mean_h*.txt")):
            raw = path.stem.removeprefix("predict_up_beta_mean_h")
            try:
                horizon = float(raw.replace("p", "."))
            except ValueError:
                continue
            variance_path = settings.models_dir / beta_logvar_filename(horizon)
            if not variance_path.is_file():
                continue
            metrics = _read_json(settings.models_dir / beta_metrics_filename(horizon))
            evaluation = _read_json(settings.models_dir / beta_eval_filename(horizon))
            rows.append(
                {
                    "id": path.name,
                    "horizon_seconds": horizon,
                    "path": str(path),
                    "modified_at": int(path.stat().st_mtime * 1000),
                    "metrics": metrics.get("test") if metrics else None,
                    "evaluation": evaluation.get("metrics") if evaluation else None,
                    "kind": "beta",
                }
            )
        return rows
    if kind != "direction":
        raise ValueError("kind must be direction or beta")
    rows: list[dict[str, Any]] = []
    for path in sorted(settings.models_dir.glob("predict_up_direction_h*.txt")):
        raw = path.stem.removeprefix("predict_up_direction_h")
        try:
            horizon = float(raw.replace("p", "."))
        except ValueError:
            continue
        metrics = _read_json(settings.models_dir / direction_metrics_filename(horizon))
        evaluation = _read_json(settings.models_dir / direction_eval_filename(horizon))
        rows.append(
            {
                "id": path.name,
                "horizon_seconds": horizon,
                "path": str(path),
                "modified_at": int(path.stat().st_mtime * 1000),
                "metrics": metrics.get("test") if metrics else None,
                "evaluation": evaluation.get("metrics") if evaluation else None,
                "kind": "direction",
            }
        )
    return rows


def active_kind() -> str:
    configured = _read_json(_ACTIVE_FILE) or {}
    kind = str(configured.get("kind") or "direction")
    return kind if kind in {"direction", "beta"} else "direction"


def active_horizons(*, kind: str | None = None) -> tuple[float, ...]:
    family = kind or active_kind()
    configured = _read_json(_ACTIVE_FILE) or {}
    if family == active_kind():
        raw = configured.get("horizons")
        if isinstance(raw, list):
            selected = tuple(float(v) for v in raw if float(v) > 0)
            if selected:
                return selected
    available = tuple(row["horizon_seconds"] for row in available_models(kind=family))
    return available or (3.0, 5.0)


def select_live_models(*, kind: str, horizons: list[float]) -> dict[str, Any]:
    """Persist which model family and horizons drive the live Prediction feed.

    For Beta/level density families, any t in (0, 60] is allowed once at least one
    artifact exists — missing horizons are interpolated by ``future_up_price_pdf``.
    Direction classifiers still require an exact trained artifact per horizon.
    """
    if kind not in {"direction", "beta"}:
        raise ValueError("kind must be direction or beta")
    selected = tuple(sorted({float(v) for v in horizons if 0 < float(v) <= 60}))
    if not selected:
        raise ValueError("Select at least one horizon in (0, 60] seconds")

    if kind == "beta":
        from app.ml.price_distribution import available_pdf_horizons
        from app.ml.train_predict_up_beta_ct import continuous_model_ready

        if not continuous_model_ready() and not available_pdf_horizons("beta"):
            raise ValueError("No trained beta models available yet (train continuous-t or a fixed horizon)")
    else:
        available = {float(row["horizon_seconds"]) for row in available_models(kind=kind)}
        if not available:
            raise ValueError(f"No trained {kind} models available yet")
        missing = [h for h in selected if h not in available]
        if missing:
            raise ValueError(
                f"Direction models missing for horizons: {', '.join(f'{h:g}s' for h in missing)}"
            )

    settings.models_dir.mkdir(parents=True, exist_ok=True)
    payload = {"kind": kind, "horizons": list(selected)}
    _ACTIVE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"kind": kind, "horizons": list(selected), "active_horizons": list(selected)}


def select_horizons(horizons: list[float]) -> tuple[float, ...]:
    return tuple(select_live_models(kind=active_kind(), horizons=horizons)["horizons"])


def job_status() -> dict[str, Any] | None:
    with _LOCK:
        return dict(_JOB) if _JOB is not None else None


def start_job(
    *, action: str, horizon_seconds: float, min_move: float, max_markets: int | None, kind: str = "direction"
) -> dict[str, Any]:
    """Start one train/evaluate job; concurrent training corrupts shared artifacts."""
    if action not in {"train", "evaluate"} or kind not in {"direction", "beta", "beta_ct"}:
        raise ValueError("action must be train or evaluate; kind must be direction, beta, or beta_ct")
    with _LOCK:
        global _JOB
        if _JOB and _JOB.get("status") == "running":
            raise RuntimeError("A prediction model job is already running")
        _JOB = {
            "id": str(int(time.time() * 1000)),
            "action": action,
            "kind": kind,
            "horizon_seconds": float(horizon_seconds),
            "min_move": float(min_move),
            "max_markets": max_markets,
            "status": "running",
            "started_at": int(time.time() * 1000),
            "progress": 0,
            "message": "Starting…",
        }

    def progress(payload: dict[str, Any]) -> None:
        with _LOCK:
            if _JOB:
                _JOB["progress"] = int(payload.get("progress") or _JOB["progress"])
                _JOB["message"] = str(payload.get("message") or _JOB["message"])

    def run() -> None:
        try:
            if kind == "beta_ct" and action == "train":
                result = train_beta_continuous(
                    max_markets=max_markets,
                    progress_cb=progress,
                )
                summary = result.get("metrics", {}).get("test")
            elif kind == "beta_ct":
                result = evaluate_beta_continuous(max_markets=max_markets)
                summary = result.get("metrics")
            elif kind == "beta" and action == "train":
                result = train_beta_horizon(
                    horizon_seconds=float(horizon_seconds),
                    max_markets=max_markets,
                    progress_cb=progress,
                )
                summary = result.get("metrics", {}).get("test")
            elif kind == "beta":
                result = evaluate_beta_horizon(
                    horizon_seconds=float(horizon_seconds),
                    max_markets=max_markets,
                )
                summary = result.get("metrics")
            elif action == "train":
                result = train_predict_up_direction_horizon(
                    horizon_seconds=float(horizon_seconds),
                    min_move=float(min_move),
                    max_markets=max_markets,
                    progress_cb=progress,
                )
                summary = result.get("metrics", {}).get("test")
            else:
                result = evaluate_predict_up_direction(
                    horizon_seconds=float(horizon_seconds),
                    min_move=float(min_move),
                    max_markets=max_markets,
                )
                summary = result.get("metrics")
            with _LOCK:
                if _JOB:
                    _JOB.update(status="completed", progress=100, message="Completed", result=summary)
        except Exception as exc:
            with _LOCK:
                if _JOB:
                    _JOB.update(status="failed", message=str(exc))

    thread_name = f"{kind}-{action}-{_horizon_tag(horizon_seconds if kind != 'beta_ct' else 0)}"
    threading.Thread(target=run, name=thread_name, daemon=True).start()
    return job_status() or {}
