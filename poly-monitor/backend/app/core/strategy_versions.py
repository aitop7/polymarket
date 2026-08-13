"""Per-strategy timestamped version files (params + trained artifacts)."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import POLY_MONITOR_ROOT, settings
from app.strategies.catalog import STRATEGY_DOCS, catalog_strategy

_VERSION_ID_RE = re.compile(r"^\d{8}_\d{6}Z$")
_SAFE_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def _versions_root() -> Path:
    root = POLY_MONITOR_ROOT / "data" / "strategy_versions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def strategy_dir(name: str) -> Path:
    key = _safe_name(name)
    d = _versions_root() / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(name: str) -> str:
    key = (name or "").strip().lower()
    if not _SAFE_NAME_RE.match(key):
        raise ValueError(f"Invalid strategy name: {name}")
    if key in {"none", "catalog", "lgbm", "versions"}:
        raise ValueError(f"Reserved strategy name: {name}")
    return key


def _new_version_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")


def _manifest_path(name: str, version_id: str) -> Path:
    return strategy_dir(name) / f"{version_id}.json"


def _active_path(name: str) -> Path:
    return strategy_dir(name) / "active.json"


def default_runtime_params(name: str) -> dict[str, Any]:
    row = catalog_strategy(name)
    if row and isinstance(row.get("runtime_params"), dict):
        return dict(row["runtime_params"])
    docs = STRATEGY_DOCS.get(_safe_name(name), {})
    return dict(docs.get("runtime_params") or {})


def list_versions(name: str) -> dict[str, Any]:
    key = _safe_name(name)
    d = strategy_dir(key)
    active = _read_json(_active_path(key)) or {}
    active_id = str(active.get("version_id") or "") or None
    versions: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.json"), reverse=True):
        if path.name == "active.json":
            continue
        vid = path.stem
        if not _VERSION_ID_RE.match(vid):
            continue
        meta = _read_json(path) or {}
        versions.append(
            {
                "id": vid,
                "strategy": key,
                "created_at": meta.get("created_at"),
                "label": meta.get("label") or "",
                "kind": meta.get("kind") or "params",
                "runtime_params": meta.get("runtime_params") or {},
                "train_params": meta.get("train_params") or {},
                "metrics_summary": meta.get("metrics_summary"),
                "has_model": bool((meta.get("artifacts") or {}).get("model")),
                "path": str(path),
                "active": vid == active_id,
            }
        )
    return {
        "strategy": key,
        "dir": str(d),
        "active_version_id": active_id,
        "count": len(versions),
        "versions": versions,
    }


def get_version(name: str, version_id: str) -> dict[str, Any]:
    key = _safe_name(name)
    vid = (version_id or "").strip()
    if not _VERSION_ID_RE.match(vid):
        raise ValueError("version_id must look like YYYYMMDD_HHMMSSZ")
    path = _manifest_path(key, vid)
    meta = _read_json(path)
    if meta is None:
        raise FileNotFoundError(f"Version not found: {key}/{vid}")
    artifacts = dict(meta.get("artifacts") or {})
    resolved = {}
    for ak, rel in artifacts.items():
        if not rel:
            continue
        p = strategy_dir(key) / str(rel)
        resolved[ak] = {"path": str(p), "exists": p.is_file()}
    return {
        **meta,
        "id": vid,
        "strategy": key,
        "path": str(path),
        "artifacts_resolved": resolved,
        "active": ((_read_json(_active_path(key)) or {}).get("version_id") == vid),
    }


def save_version(
    name: str,
    *,
    runtime_params: dict[str, Any] | None = None,
    train_params: dict[str, Any] | None = None,
    label: str | None = None,
    kind: str = "params",
    metrics: dict[str, Any] | None = None,
    copy_lgbm_artifacts: bool = False,
    make_active: bool = True,
) -> dict[str, Any]:
    """Write a timestamped version file (and optional model artifacts)."""
    key = _safe_name(name)
    vid = _new_version_id()
    d = strategy_dir(key)
    created = datetime.now(timezone.utc).isoformat()

    rt = dict(runtime_params if runtime_params is not None else default_runtime_params(key))
    tr = dict(train_params or {})
    artifacts: dict[str, str] = {}

    metrics_summary = None
    if metrics and isinstance(metrics, dict) and "error" not in metrics:
        metrics_summary = {
            "best_iteration": metrics.get("best_iteration"),
            "n_features": metrics.get("n_features"),
            "train": metrics.get("train"),
            "validation": metrics.get("validation"),
            "test": metrics.get("test"),
        }
        metrics_path = d / f"{vid}.metrics.json"
        _write_json(metrics_path, metrics)
        artifacts["metrics"] = metrics_path.name

    if copy_lgbm_artifacts and key == "lgbm_edge":
        src_model = settings.models_dir / "lgbm_baseline.txt"
        src_features = settings.models_dir / "feature_names.json"
        src_metrics = settings.models_dir / "metrics.json"
        if src_model.is_file():
            dest = d / f"{vid}.model.txt"
            shutil.copy2(src_model, dest)
            artifacts["model"] = dest.name
            rt["model_path"] = str(dest)
        if src_features.is_file():
            dest_f = d / f"{vid}.features.json"
            shutil.copy2(src_features, dest_f)
            artifacts["features"] = dest_f.name
        if "metrics" not in artifacts and src_metrics.is_file():
            dest_m = d / f"{vid}.metrics.json"
            shutil.copy2(src_metrics, dest_m)
            artifacts["metrics"] = dest_m.name
            try:
                m = json.loads(dest_m.read_text(encoding="utf-8"))
                if isinstance(m, dict):
                    metrics_summary = {
                        "best_iteration": m.get("best_iteration"),
                        "n_features": m.get("n_features"),
                        "train": m.get("train"),
                        "validation": m.get("validation"),
                        "test": m.get("test"),
                    }
            except Exception:
                pass

    manifest = {
        "id": vid,
        "strategy": key,
        "created_at": created,
        "label": (label or "").strip(),
        "kind": kind if kind in {"params", "train"} else "params",
        "runtime_params": rt,
        "train_params": tr,
        "metrics_summary": metrics_summary,
        "artifacts": artifacts,
    }
    path = _manifest_path(key, vid)
    _write_json(path, manifest)

    if make_active:
        _set_active(key, vid, note="saved")

    return get_version(key, vid)


def activate_version(name: str, version_id: str) -> dict[str, Any]:
    """Load a version: restore lgbm artifacts into models_dir and mark active."""
    key = _safe_name(name)
    meta = get_version(key, version_id)
    artifacts = dict(meta.get("artifacts") or {})
    d = strategy_dir(key)

    if key == "lgbm_edge" and artifacts.get("model"):
        src = d / str(artifacts["model"])
        if not src.is_file():
            raise FileNotFoundError(f"Model artifact missing: {src}")
        settings.models_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, settings.models_dir / "lgbm_baseline.txt")
        if artifacts.get("metrics"):
            msrc = d / str(artifacts["metrics"])
            if msrc.is_file():
                shutil.copy2(msrc, settings.models_dir / "metrics.json")
        if artifacts.get("features"):
            fsrc = d / str(artifacts["features"])
            if fsrc.is_file():
                shutil.copy2(fsrc, settings.models_dir / "feature_names.json")

    _set_active(key, version_id, note="activated")
    return get_version(key, version_id)


def get_active(name: str) -> dict[str, Any]:
    key = _safe_name(name)
    active = _read_json(_active_path(key)) or {}
    vid = active.get("version_id")
    if vid:
        try:
            return {
                "strategy": key,
                "active": True,
                "version": get_version(key, str(vid)),
            }
        except FileNotFoundError:
            pass
    return {
        "strategy": key,
        "active": False,
        "version": {
            "id": None,
            "strategy": key,
            "runtime_params": default_runtime_params(key),
            "train_params": dict(
                (catalog_strategy(key) or {}).get("train_defaults") or {}
            ),
            "kind": "defaults",
            "label": "catalog defaults",
        },
    }


def _set_active(name: str, version_id: str, *, note: str = "") -> None:
    _write_json(
        _active_path(name),
        {
            "strategy": name,
            "version_id": version_id,
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "note": note,
        },
    )


def save_train_result(
    *,
    runtime_params: dict[str, Any] | None = None,
    train_params: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Snapshot after a successful LightGBM train."""
    rt = runtime_params
    if rt is None:
        try:
            active = get_active("lgbm_edge")
            ver = active.get("version") or {}
            if isinstance(ver.get("runtime_params"), dict):
                rt = dict(ver["runtime_params"])
        except Exception:
            rt = None
    return save_version(
        "lgbm_edge",
        runtime_params=rt,
        train_params=train_params,
        label=label or "train",
        kind="train",
        metrics=metrics,
        copy_lgbm_artifacts=True,
        make_active=True,
    )
