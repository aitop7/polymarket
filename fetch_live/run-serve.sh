#!/usr/bin/env bash
# Run on the VPS (Linux): fetch_live read-only HTTP API for local poly-monitor sync.
# Usage:
#   cd /path/to/fetch_live
#   chmod +x run-serve.sh
#   ./run-serve.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

export FETCH_LIVE_SERVE_HOST="${FETCH_LIVE_SERVE_HOST:-0.0.0.0}"
export FETCH_LIVE_SERVE_PORT="${FETCH_LIVE_SERVE_PORT:-8787}"

if [[ -x "$ROOT/../venv/bin/python" ]]; then
  PY="$ROOT/../venv/bin/python"
elif [[ -x "$ROOT/venv/bin/python" ]]; then
  PY="$ROOT/venv/bin/python"
else
  PY="python3"
fi

exec "$PY" "$ROOT/serve.py"
