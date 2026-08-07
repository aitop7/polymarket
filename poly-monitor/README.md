# poly-monitor

Historical monitor, backtest, and paper trading for Polymarket BTC Up/Down 5m markets.

Uses datasets from `../fetch_real` (`training/`, `features/`, `models/`). **No live trading in v1.**

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

Terminal 2 — UI:

```bash
cd poly-monitor/frontend
npm run dev
```

Open http://localhost:5173

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
