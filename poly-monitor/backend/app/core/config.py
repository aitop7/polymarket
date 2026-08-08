from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# poly-monitor/backend/app/core/config.py -> parents[3] = poly-monitor, parents[4] = repo root
_POLY_MONITOR_ROOT = Path(__file__).resolve().parents[3]
_REPO_ROOT = _POLY_MONITOR_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    fetch_real_root: Path = _REPO_ROOT / "fetch_real"
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
    def models_dir(self) -> Path:
        return self.fetch_real_root / "models"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
