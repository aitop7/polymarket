from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import const


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = const.DATA_DIR
    parquet_compression: str = const.PARQUET_COMPRESSION

    binance_ws_url: str = const.BINANCE_WS_URL
    binance_rest_url: str = const.BINANCE_REST_URL
    binance_rest_fallbacks: str = const.BINANCE_REST_FALLBACKS

    polymarket_gamma_url: str = const.POLYMARKET_GAMMA_URL
    polymarket_clob_url: str = const.POLYMARKET_CLOB_URL
    polymarket_ws_url: str = const.POLYMARKET_WS_URL

    pmxt_api_key: str = ""
    use_pmxt: bool = const.USE_PMXT

    btc_symbol: str = const.BTC_SYMBOL
    market_slug_prefix: str = const.MARKET_SLUG_PREFIX

    orderbook_interval_ms: int = const.ORDERBOOK_INTERVAL_MS
    market_discovery_interval_s: int = const.MARKET_DISCOVERY_INTERVAL_S
    metadata_refresh_interval_s: int = const.METADATA_REFRESH_INTERVAL_S
    whale_trade_threshold: float = const.WHALE_TRADE_THRESHOLD
    history_lookback_days: int = const.HISTORY_LOOKBACK_DAYS

    market_concurrency: int = const.MARKET_CONCURRENCY
    download_concurrency: int = const.DOWNLOAD_CONCURRENCY

    raw_data_dir: Path = const.RAW_DATA_DIR
    log_level: str = const.LOG_LEVEL

    @property
    def pmxt_enabled(self) -> bool:
        return bool(self.use_pmxt and self.pmxt_api_key.strip())

    @property
    def polymarket_market_ws(self) -> str:
        return f"{self.polymarket_ws_url.rstrip('/')}/ws/market"

    @property
    def binance_trade_stream(self) -> str:
        return f"{self.binance_ws_url.rstrip('/')}/{self.btc_symbol.lower()}@trade"

    @property
    def binance_book_ticker_stream(self) -> str:
        return f"{self.binance_ws_url.rstrip('/')}/{self.btc_symbol.lower()}@bookTicker"

    @property
    def binance_rest_bases(self) -> list[str]:
        bases = [self.binance_rest_url, *self.binance_rest_fallbacks.split(",")]
        return [b.strip().rstrip("/") for b in bases if b.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
