from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FETCH_LIVE_",
        extra="ignore",
    )

    data_dir: Path = Path(__file__).resolve().parents[2] / "data"
    flush_interval_s: float = 7.0
    flush_max_rows: int = 1000
    parquet_compression: str = "zstd"

    market_duration_s: int = 300
    discovery_interval_s: float = 5.0
    resolve_poll_interval_s: float = 10.0
    snapshot_interval_s: float = 1.0

    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    polymarket_market_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    rtds_url: str = "wss://ws-live-data.polymarket.com"

    binance_ws_url: str = "wss://data-stream.binance.vision"
    binance_rest_url: str = "https://data-api.binance.vision"
    binance_ws_fallbacks: tuple[str, ...] = (
        "wss://data-stream.binance.vision",
        "wss://stream.binance.com:9443",
        "wss://stream.binance.com:443",
    )
    btc_symbol: str = "BTCUSDT"


settings = Settings()
