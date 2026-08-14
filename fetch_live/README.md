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
FETCH_LIVE_API_TOKEN=change-me
FETCH_LIVE_SERVE_PORT=8787
FETCH_LIVE_TRADES_MODE=full   # full = taker+maker; taker = takers only (faster)
```

## Run collector

```bash
python main.py
```

Graceful shutdown (SIGINT/SIGTERM) flushes all buffers before exit.

Binance streams default to `wss://data-stream.binance.vision` (geo-friendly). Override with `FETCH_LIVE_BINANCE_WS_URL` if needed.

## Run VPS data API (serve saved markets)

On the VPS (same machine / same `FETCH_LIVE_DATA_DIR` as the collector):

On the VPS (Linux), keep the collector (`python main.py`) running, and start the API separately.

Repo path on VPS: `/root/charles/fetch_live/`

```bash
cd /root/charles/fetch_live
source /root/charles/venv/bin/activate   # or your venv path
pip install -r requirements.txt
chmod +x run-serve.sh
export FETCH_LIVE_DATA_DIR=/root/charles/fetch_live/data
export FETCH_LIVE_SERVE_HOST=0.0.0.0
export FETCH_LIVE_SERVE_PORT=8787
./run-serve.sh
```

Or install a systemd service:

```bash
sudo cp /root/charles/fetch_live/deploy/fetch-live-serve.service /etc/systemd/system/
# if python is not at /root/charles/venv/bin/python, edit ExecStart first
sudo systemctl daemon-reload
sudo systemctl enable --now fetch-live-serve
sudo ufw allow 8787/tcp
curl -s http://127.0.0.1:8787/health
```

Verify from your PC: `curl http://YOUR_VPS:8787/health` → `{"ok":true,...}`.
Open firewall TCP **8787** if needed.

Endpoints (Bearer token required when `FETCH_LIVE_API_TOKEN` is set):

| Path | Purpose |
|------|---------|
| `GET /health` | liveness + data_dir + `trades_mode` |
| `GET /settings` | current trade capture mode |
| `PUT /settings` | switch `taker` / `full` without restarting the collector |
| `GET /markets?after_start_ms=` | catalog (incremental) |
| `GET /markets/{id}` | meta + file sizes |
| `GET /markets/{id}/files/{name}` | single file download |
| `POST /markets/{id}/repair` | fill missed trades from Data API (history markets) |
| `GET /markets/{id}/archive` | zip of market dir |

Trade capture mode (collector + serve share `{data_dir}/collector_settings.json`):

| Mode | Data API | Saved rows |
|------|----------|------------|
| `taker` | `takerOnly=true` only (one request) | taker fills only — faster |
| `full` | `takerOnly=false` + `takerOnly=true` | taker + maker (default) |

Switch while both processes are running (collector picks it up on the next seed / end fill / flush):

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"trades_mode":"taker"}' http://VPS:8787/settings
```

Repair a finished market (fills missed trades, then poly-monitor can re-pull):

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://VPS:8787/markets/MARKET_ID/repair
```

Full request/response docs: **[API.md](API.md)**. Live OpenAPI: `http://HOST:8787/docs`.

For a dedicated OpenAPI service (typed `/meta`, JSON `/tables/{table}`, optional upstream sync), see sibling **[`../data_service`](../data_service)** (drop-in compatible with poly-monitor `VPS_SYNC_URL`).

Local poly-monitor pulls these into `FETCH_LIVE_DATA_DIR` (see poly-monitor `.env`).

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
      meta.json                        # includes btc_open_price / btc_close_price (60s TWAP)
```

`meta.json`: `btc_open_price` = Chainlink 60s TWAP at window start (Price to Beat); `btc_close_price` = Chainlink 60s TWAP at window end (set when the market rolls off).

`chainlink_price.parquet`: Chainlink spot (`Chainlink_BTC`) and 60s TWAP from Polymarket RTDS.

`binance_price_orderbook.parquet`: Binance mid (`Binance_BTC`) plus ask/bid BTC quantity in USD-distance bands from mid (widths 0.1, 0.2, …, 51.2 → `ask_0_1` … `ask_511_1023`, plus out-of-range `ask_1023_` / `bid_1023_`).

`trades.parquet` is streamed from Polymarket RTDS (`activity` / `trades`) plus Data API
gap-fill. In `full` mode each row is **one wallet’s fill** (taker or maker). In `taker`
mode only taker rows are fetched and saved. One `transaction_hash` can appear on several
rows.

| Column | Meaning |
|--------|---------|
| `timestamp` | fill time (ms) |
| `transaction_hash` | Polygon tx id |
| `wallet` | proxy wallet for this fill |
| `is_up` | true = Up outcome, false = Down (**per fill**, Orbscan outcome) |
| `is_buy` | true = Buy, false = Sell (**per fill**, Orbscan action) |
| `is_taker` | true = Taker, false = Maker |
| `price` / `shares` / `fill_index` | **per-fill** price and size; `fill_index` distinguishes identical legs (same wallet/price/shares) so Orbscan duplicate maker fills are kept |

CLOB WS is used for live order books only. Data API `/trades` seeds on market start and
gap-fills on market end. `full` uses `takerOnly=false` + taker-set classification;
`taker` uses `takerOnly=true` only. Rows follow **Orbscan** semantics: each action is
its own row (wallets are not collapsed).
