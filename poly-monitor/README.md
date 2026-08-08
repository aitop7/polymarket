# poly-monitor

Historical monitor, backtest, and paper trading for Polymarket BTC Up/Down 5m markets.

Uses datasets from `../fetch_real` (`training/`, `features/`, `models/`).

**Live trading** (left sidebar) shows a **view-only** feed of the current 5m market (Binance BTC + Polymarket CLOB Up/Down + order book). It does **not** place live orders.

## Setup

```bash
# from repo root, reuse existing venv
.\venv\Scripts\activate
pip install -r poly-monitor/requirements.txt

# frontend
cd poly-monitor/frontend
npm install
```

## Run

Terminal 1 — API:

```bash
cd poly-monitor
.\run-api.bat
```

(`run-api.bat` loads `.env`, sets `PYTHONPATH`, and starts uvicorn on `127.0.0.1:8000`.)

Or manually:

```bash
cd poly-monitor/backend
set PYTHONPATH=%CD%;%CD%\..
..\..\venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Terminal 2 — UI (dev):

```bash
cd poly-monitor/frontend
npm run dev
```

Open http://localhost:5173

### Built UI

```bash
cd poly-monitor/frontend
npm run build
```

Then either:

- Open **http://127.0.0.1:8000** (API serves `frontend/dist`), or
- `npm run preview` → http://localhost:4173 (proxies `/api` to the backend)

## Strategies

Plugins live in `poly-monitor/strategies/`:

- `edge_threshold` — trade when `model_p_up - market` exceeds threshold
- `lgbm_edge` — LightGBM baseline + edge rule

Implement `on_tick` / `on_market_end` per `strategies/base.py` and register in `backend/app/strategies/registry.py`.

## Env

Copy `.env.example` → `.env` (loaded from `poly-monitor/.env`).

| Variable | Default |
|----------|---------|
| `FETCH_LIVE_DATA_DIR` | `E:\DataSets\poly\live` — local mirror of VPS `fetch_live` markets |
| `VPS_SYNC_URL` | empty (sync off). Example: `http://YOUR_VPS:8787` |
| `VPS_SYNC_TOKEN` | must match VPS `FETCH_LIVE_API_TOKEN` |
| `FETCH_REAL_ROOT` | `../fetch_real` (relative to cwd; absolute path recommended) |
| `CORS_ORIGINS` | `http://localhost:5173` |

### VPS → local live sync

`fetch_live` runs on the VPS only. Local poly-monitor:

1. On startup: pulls all markets with `start_time` after the watermark in `backend/.cache/fetch_live_sync.json`
2. On market rollover: re-pulls the just-closed market
3. Mid-window: periodically refreshes the current market so chart backfill has the VPS prefix
4. Historical / replay / paper: **local disk only** (no VPS calls)

Synced layout: `{FETCH_LIVE_DATA_DIR}/YYYY-MM-DD/{market_id}/`.
