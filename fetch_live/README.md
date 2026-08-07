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
      binance_trades.parquet           # Binance aggTrades
      binance_price_orderbook.parquet  # 1s Binance_BTC mid + USD-distance qty bands
      chainlink_price.parquet          # 1s Chainlink_BTC + twap (RTDS)
      orderbooks.parquet               # Polymarket Up/Down books
      trades.parquet                   # RTDS activity/trades (includes wallet)
      meta.json                        # includes btc_open_price / btc_close_price (30s TWAP)
```

`meta.json`: `btc_open_price` = Chainlink 30s TWAP at window start (Price to Beat); `btc_close_price` = Chainlink 30s TWAP at window end (set when the market rolls off).

`chainlink_price.parquet`: Chainlink spot (`Chainlink_BTC`) and 30s TWAP from Polymarket RTDS.

`binance_price_orderbook.parquet`: Binance mid (`Binance_BTC`) plus ask/bid BTC quantity in USD-distance bands from mid (widths 0.1, 0.2, …, 51.2 → `ask_0_1` … `ask_511_1023`, plus out-of-range `ask_1023_` / `bid_1023_`).

`trades.parquet` is streamed from Polymarket RTDS (`activity` / `trades`) with `proxyWallet`. CLOB WS is used for live order books only. Data API `/trades` seeds on market start and gap-fills on market end.
