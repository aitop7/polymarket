# fetch_live HTTP API

FastAPI over the collector’s `FETCH_LIVE_DATA_DIR`. Used by local poly-monitor (and any client) to pull saved BTC Up/Down 5m market bundles from a VPS. `PUT /settings` can switch trade capture mode without restarting the collector.

**Implementation:** [`app/serve_api.py`](app/serve_api.py) · **Entry:** `python serve.py` / `./run-serve.sh`  
**Default base URL:** `http://HOST:8787`

---

## Run

```bash
cd fetch_live
export FETCH_LIVE_DATA_DIR=/path/to/data
export FETCH_LIVE_SERVE_HOST=0.0.0.0
export FETCH_LIVE_SERVE_PORT=8787
export FETCH_LIVE_API_TOKEN=change-me   # optional; empty = no auth
python serve.py
```

Interactive OpenAPI UI (when the server is up):

- Swagger: `http://HOST:8787/docs`
- ReDoc: `http://HOST:8787/redoc`

---

## Auth

| `FETCH_LIVE_API_TOKEN` | Behavior |
|------------------------|----------|
| empty / unset | No auth (dev only) |
| set | All routes except `/health` require `Authorization: Bearer <token>` |

```http
Authorization: Bearer change-me
```

| Status | Meaning |
|--------|---------|
| `401` | Missing or invalid bearer token |
| `404` | Market or file not found |
| `400` | Invalid file name on `/files/{name}`, or invalid `trades_mode` |

---

## Endpoints

### `GET /health`

Liveness. **No auth.**

**Response**

```json
{
  "ok": true,
  "data_dir": "/root/charles/fetch_live/data",
  "exists": true,
  "trades_mode": "full"
}
```

```bash
curl -s http://127.0.0.1:8787/health
```

---

### `GET /settings`

Current trade capture mode. Auth required when a token is set.

**Response**

```json
{
  "trades_mode": "taker",
  "source": "file",
  "options": ["taker", "full"]
}
```

| Field | Meaning |
|-------|---------|
| `trades_mode` | `taker` = save takers only (faster Data API); `full` = taker + maker |
| `source` | `file` = runtime override in `{data_dir}/collector_settings.json`; `env` = `FETCH_LIVE_TRADES_MODE` |

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://VPS:8787/settings
```

---

### `PUT /settings`

Switch mode while the collector is running. Writes `{data_dir}/collector_settings.json`; the collector reads it on the next seed / end fill / flush. Applies to **new** fetches; already-written parquet is not rewritten.

**Body**

```json
{ "trades_mode": "taker" }
```

Aliases accepted: `taker` / `taker_only` / `takers` · `full` / `all` / `maker`.

**Response:** same shape as `GET /settings` plus `"ok": true`. Invalid value → `400`.

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"trades_mode":"taker"}' http://VPS:8787/settings

curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"trades_mode":"full"}' http://VPS:8787/settings
```

---

### `POST /markets/{market_id}/repair`

Fill missed Polymarket trades on the VPS. Fetches Data API `/trades` (respects current `trades_mode`), merges into `trades.parquet`, stamps `meta.json` (`trades_repaired_at`, `trades_count`, `trades_mode`).

Refuses while the market is still live (`409`) so the collector cannot overwrite the repair on flush.

**Response**

```json
{
  "ok": true,
  "market_id": "3403562",
  "trades_mode": "full",
  "rows_before": 412,
  "rows_from_api": 860,
  "rows_after": 860,
  "rows_added": 448,
  "repaired_at": 1786207454000,
  "market": { }
}
```

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://VPS:8787/markets/3403562/repair
```

---

### `GET /markets`

Catalog of markets under `data_dir` (`YYYY-MM-DD/{market_id}/` with valid `meta.json`).

**Query**

| Param | Type | Description |
|-------|------|-------------|
| `after_start_ms` | int (optional) | Only markets with `start_time` **strictly greater** than this (incremental sync) |

**Response**

```json
{
  "count": 2,
  "markets": [
    {
      "market_id": "3403562",
      "date": "2026-08-08",
      "start_time": 1786206900000,
      "end_time": 1786207200000,
      "active": false,
      "closed": true,
      "slug": "btc-updown-5m-1786206900",
      "mtime_ms": 1786207454000,
      "files": [
        { "name": "meta.json", "size": 612, "mtime_ms": 1786207454000 },
        { "name": "trades.parquet", "size": 12048, "mtime_ms": 1786207454000 }
      ]
    }
  ]
}
```

Markets with `start_time` before ~2020-09 (`1_600_000_000_000` ms) are skipped. Sorted by `(start_time, market_id)`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://VPS:8787/markets"

curl -s -H "Authorization: Bearer $TOKEN" \
  "http://VPS:8787/markets?after_start_ms=1786200000000"
```

---

### `GET /markets/{market_id}`

One catalog entry (newest date folder wins if the id appears under multiple days).

**Response:** same object as one element of `markets[]` above.

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://VPS:8787/markets/3403562"
```

---

### `GET /markets/{market_id}/files/{name}`

Download a single allowed file as-is.

**Allowed `name` values**

| Name | Content-Type |
|------|----------------|
| `meta.json` | `application/json` |
| `binance_trades.parquet` | `application/octet-stream` |
| `binance_price_orderbook.parquet` | `application/octet-stream` |
| `chainlink_price.parquet` | `application/octet-stream` |
| `orderbooks.parquet` | `application/octet-stream` |
| `trades.parquet` | `application/octet-stream` |

Any other name → `400`. Missing file → `404`.

```bash
curl -sL -H "Authorization: Bearer $TOKEN" \
  -o meta.json \
  "http://VPS:8787/markets/3403562/files/meta.json"

curl -sL -H "Authorization: Bearer $TOKEN" \
  -o trades.parquet \
  "http://VPS:8787/markets/3403562/files/trades.parquet"
```

---

### `GET /markets/{market_id}/archive`

ZIP of the market directory (all present allowed files + `date.txt` with the `YYYY-MM-DD` folder name).

**Headers**

| Header | Value |
|--------|--------|
| `Content-Type` | `application/zip` |
| `Content-Disposition` | `attachment; filename="{market_id}.zip"` |
| `X-Market-Date` | `YYYY-MM-DD` |

```bash
curl -sL -H "Authorization: Bearer $TOKEN" \
  -o 3403562.zip \
  "http://VPS:8787/markets/3403562/archive"
```

---

## File payloads (on disk / via `/files` or archive)

Paths under `data/YYYY-MM-DD/{market_id}/`. See [README.md](README.md) for collector semantics.

### `meta.json`

| Field | Type | Notes |
|-------|------|--------|
| `market_id` | string | Polymarket market id |
| `condition_id` | string | Condition / market hex id |
| `slug` | string | e.g. `btc-updown-5m-{unix}` |
| `question` | string | Display title |
| `up_token_id` / `down_token_id` | string | CLOB token ids |
| `start_time` / `end_time` | int64 ms | 5m window |
| `resolved_at` | int64 ms \| null | When known |
| `btc_open_price` | float \| null | Chainlink 30s TWAP at open (Price to Beat) |
| `btc_close_price` | float \| null | Chainlink 30s TWAP at end |
| `winner` | bool \| null | `true` = Up, `false` = Down |
| `active` / `closed` | bool | Session flags |
| `trades_mode` | string \| null | `taker` or `full` when the collector stamped this market |

### Parquet tables

| File | Columns (summary) |
|------|-------------------|
| `binance_trades.parquet` | `timestamp`, `price`, `quantity`, `buyer_is_maker` |
| `binance_price_orderbook.parquet` | `timestamp`, `Binance_BTC`, ask/bid USD-distance band qty columns |
| `chainlink_price.parquet` | `timestamp`, `Chainlink_BTC`, `twap` |
| `orderbooks.parquet` | `timestamp`, Up/Down prices + best bid/ask + ¢ distance share bands |
| `trades.parquet` | `timestamp`, `transaction_hash`, `wallet`, `is_up`, `is_buy`, `is_taker`, `price`, `shares`, `fill_index` |

Schemas: [`app/schemas.py`](app/schemas.py).

---

## Env (serve-related)

| Variable | Default | Meaning |
|----------|---------|---------|
| `FETCH_LIVE_DATA_DIR` | `./data` | Root scanned by the API |
| `FETCH_LIVE_SERVE_HOST` | `0.0.0.0` | Bind address |
| `FETCH_LIVE_SERVE_PORT` | `8787` | Port |
| `FETCH_LIVE_API_TOKEN` | `""` | Bearer secret; empty disables auth |
| `FETCH_LIVE_TRADES_MODE` | `full` | Default trade capture: `taker` or `full`. Overridden by `PUT /settings` |

---

## Client notes

- Prefer `/markets?after_start_ms=` for incremental sync, then `/archive` or `/files/{name}` per market.
- poly-monitor pulls from this API into its local `FETCH_LIVE_DATA_DIR` (see poly-monitor `.env`).
- This API does **not** expose live CLOB/WS streams; it serves files written by `python main.py` plus `PUT /settings` for trade capture mode.
