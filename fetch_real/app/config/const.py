"""Project defaults / constants (overridable via .env through Settings)."""

from pathlib import Path

# Storage
DATA_DIR = Path("data")
RAW_DATA_DIR = Path("data")
PARQUET_COMPRESSION = "zstd"

# Binance
BINANCE_WS_URL = "wss://data-stream.binance.vision/ws"
BINANCE_REST_URL = "https://data-api.binance.vision"
BINANCE_REST_FALLBACKS = (
    "https://api.binance.com,https://api1.binance.com,https://api2.binance.com"
)
BTC_SYMBOL = "btcusdt"

# Polymarket
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_URL = "https://clob.polymarket.com"
POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com"
MARKET_SLUG_PREFIX = "btc-updown-5m"

# PMXT
USE_PMXT = True

# Collector intervals
ORDERBOOK_INTERVAL_MS = 1000
MARKET_DISCOVERY_INTERVAL_S = 30
METADATA_REFRESH_INTERVAL_S = 300
WHALE_TRADE_THRESHOLD = 1000.0
HISTORY_LOOKBACK_DAYS = 7

# Parallel download
MARKET_CONCURRENCY = 8
DOWNLOAD_CONCURRENCY = 16

# Logging
LOG_LEVEL = "INFO"

# Market window
MARKET_DURATION_MINUTES = 5
MARKET_SLOT_SECONDS = 300
