# Live Polymarket + Binance collector for BTC Up/Down 5m markets.

Collects lossless per-market Parquet under `data/YYYY-MM-DD/{market_id}/`.

## Setup

```bash
cd fetch_live
pip install -r requirements.txt
```

Optional `.env`:

```text
FETCH_LIVE_DATA_DIR=./data
FETCH_LIVE_FLUSH_INTERVAL_S=7
FETCH_LIVE_FLUSH_MAX_ROWS=1000
```

## Run

```bash
python main.py
```

Graceful shutdown (SIGINT/SIGTERM) flushes all buffers before exit.

Binance streams default to `wss://data-stream.binance.vision` (geo-friendly). Override with `FETCH_LIVE_BINANCE_WS_URL` if needed.

## Output

```text
data/
  YYYY-MM-DD/
    {market_id}/
      btc_trades.parquet
      btc_depth.parquet
      orderbooks.parquet
      trades.parquet
      meta.json
```
