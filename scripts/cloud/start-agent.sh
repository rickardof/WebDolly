#!/bin/bash
# Start the Fara GUI server on the VM. Reads config from scripts/cloud/fara.env.
set -euo pipefail

FARA_HOME="${FARA_HOME:-$HOME/web-agent}"
ENV_FILE="$FARA_HOME/scripts/cloud/fara.env"
PORT="${FARA_PORT:-8766}"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found."
  echo "       cp $FARA_HOME/scripts/cloud/fara.env.example $ENV_FILE  and fill it in."
  exit 1
fi

# Load env vars (FARA_HOME in the file, if any, wins for the paths it sets)
set -a; source "$ENV_FILE"; set +a

VENV="$FARA_HOME/venv_pw/bin/python"
SCRIPT="$FARA_HOME/agent/gui_server.py"
LOG="${FARA_LOG_ROOT:-$FARA_HOME/var/logs/pw}/gui_server.out"

echo "Stopping old instance (if any)..."
curl -s -X POST "http://127.0.0.1:$PORT/agent/stop" 2>/dev/null || true
sleep 1
pkill -f "gui_server.py --port $PORT" 2>/dev/null || true
sleep 1

echo "Ensuring browser container is up..."
docker start fara-browser-pw 2>/dev/null || true
sleep 2

echo "Starting gui_server on :$PORT ..."
mkdir -p "$(dirname "$LOG")"
cd "$FARA_HOME/agent"
setsid nohup "$VENV" "$SCRIPT" --port "$PORT" > "$LOG" 2>&1 < /dev/null &
disown

sleep 3
if curl -s -o /dev/null --max-time 4 "http://127.0.0.1:$PORT/agent/status"; then
  echo "OK — gui_server running at http://127.0.0.1:$PORT"
  echo "To reach it from your PC: ssh -L $PORT:localhost:$PORT $(whoami)@YOUR_VM_IP"
else
  echo "WARNING — server not responding. Log:"
  tail -20 "$LOG"
fi
