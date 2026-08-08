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
cd poly-monitor/backend
# PYTHONPATH includes backend + poly-monitor (strategies)
set PYTHONPATH=%CD%;%CD%\..
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Or use `poly-monitor/run-api.bat`.

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

| Variable | Default |
|----------|---------|
| `FETCH_REAL_ROOT` | `../fetch_real` (relative to cwd; absolute path recommended) |
| `CORS_ORIGINS` | `http://localhost:5173` |
