#!/usr/bin/env bash
# Starts the control API (127.0.0.1:8711) and dashboard (127.0.0.1:8502) on Linux/macOS.
# A control token is generated per user if none is set; it is never printed.
set -euo pipefail
API_PORT="${1:-8711}"; DASH_PORT="${2:-8502}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3
TOKEN_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/deep-research/control.token"
T="${DEEP_RESEARCH_CONTROL_TOKEN:-}"
if [ "${#T}" -lt 16 ]; then
  if [ ! -s "$TOKEN_FILE" ]; then
    mkdir -p "$(dirname "$TOKEN_FILE")"; umask 077
    "$PY" -c "import secrets;print(secrets.token_urlsafe(32),end='')" > "$TOKEN_FILE"
  fi
  DEEP_RESEARCH_CONTROL_TOKEN="$(cat "$TOKEN_FILE")"
fi
export DEEP_RESEARCH_CONTROL_TOKEN DEEP_RESEARCH_CONTROL_URL="http://127.0.0.1:$API_PORT" PYTHONPATH="$ROOT/src"
mkdir -p logs
nohup "$PY" -P -m deep_research.control.server --port "$API_PORT" > logs/control.out.log 2> logs/control.err.log &
nohup "$PY" -P -m streamlit run src/deep_research/dashboard/app.py --server.address 127.0.0.1 --server.port "$DASH_PORT" \
  --server.headless true --theme.base light > logs/dashboard.out.log 2> logs/dashboard.err.log &
sleep 4
echo "Dashboard: http://127.0.0.1:$DASH_PORT  (control API on $API_PORT)"
