from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# poly-monitor/backend/app/core/config.py -> parents[3] = poly-monitor, parents[4] = repo root
_POLY_MONITOR_ROOT = Path(__file__).resolve().parents[3]
_REPO_ROOT = _POLY_MONITOR_ROOT.parent
_DEFAULT_LIVE_DIR = Path(r"E:\DataSets\poly\live")


# Re-export for modules that need the poly-monitor root path.
POLY_MONITOR_ROOT = _POLY_MONITOR_ROOT


def normalize_vps_sync_url(raw: str) -> str:
    """Accept host:port or full URL; return absolute http(s) base or ''."""
    text = (raw or "").strip().rstrip("/")
    if not text or text.lower() in {"none", "null", "off", "false", "0"}:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    lower = text.lower()
    if not (lower.startswith("http://") or lower.startswith("https://")):
        return ""
    # Reject obvious placeholders
    if "your_vps" in lower or "example.com" in lower:
        return ""
    return text.rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_POLY_MONITOR_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    fetch_real_root: Path = _REPO_ROOT / "fetch_real"
    fetch_live_data_dir: Path = _DEFAULT_LIVE_DIR
    vps_sync_url: str = ""
    vps_sync_token: str = ""
    # Shared fallback when purpose-specific keys are unset.
    pmdata_api_key: str = ""
    pmdata_api_key_books: str = ""
    pmdata_api_key_chainlink: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:4173,http://127.0.0.1:4173"
    )
    default_cash: float = 1000.0

    @property
    def training_dir(self) -> Path:
        return self.fetch_real_root / "training"

    @property
    def features_dir(self) -> Path:
        return self.fetch_real_root / "features"

    @property
    def features_live_dir(self) -> Path:
        """Engineered live features for predict_up / direction / beta training."""
        return self.fetch_real_root / "features_live"

    @property
    def models_dir(self) -> Path:
        return self.fetch_real_root / "models"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def vps_sync_base_url(self) -> str:
        return normalize_vps_sync_url(self.vps_sync_url)

    @property
    def vps_sync_enabled(self) -> bool:
        return bool(self.vps_sync_base_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
