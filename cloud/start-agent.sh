#!/bin/bash
# Start the Fara GUI server on the cloud VM.
# Reads config from ~/web-agent/cloud/fara.env
set -euo pipefail

ENV_FILE=~/web-agent/cloud/fara.env
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found. Copy and fill in cloud/fara.env first."
  exit 1
fi

# Load env vars
set -a; source "$ENV_FILE"; set +a

VENV=~/web-agent/venv_pw/bin/python
SCRIPT=~/web-agent/controller_pw/gui_server.py
LOG=~/web-agent/logs_pw/gui_server.out
PORT=8766

echo "Stopping old instance (if any)..."
curl -s -X POST "http://127.0.0.1:$PORT/agent/stop" 2>/dev/null || true
sleep 1
pkill -f "gui_server.py --port $PORT" 2>/dev/null || true
sleep 1

echo "Ensuring browser container is up..."
docker start fara-browser-pw 2>/dev/null || true
sleep 2

echo "Starting gui_server on :$PORT ..."
mkdir -p ~/web-agent/logs_pw
setsid nohup $VENV $SCRIPT --port $PORT > "$LOG" 2>&1 < /dev/null &
disown

sleep 3
if curl -s -o /dev/null --max-time 4 "http://127.0.0.1:$PORT/agent/status"; then
  echo "OK — gui_server running at http://127.0.0.1:$PORT"
  echo "To access from your PC: ssh -L 8766:localhost:8766 $(whoami)@YOUR_VM_IP"
else
  echo "WARNING — server non risponde. Log:"
  tail -20 "$LOG"
fi
