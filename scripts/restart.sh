#!/bin/bash
# Riavvia il GUI server per applicare modifiche al codice Python.
# Ferma l'agente se in esecuzione, uccide il vecchio processo, rilancia.
#
# Tutti i percorsi vengono dalla posizione di questo script: la cartella del
# progetto si può spostare o rinominare senza toccare niente.
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/venv_pw/bin/python"
SCRIPT="$ROOT/agent/gui_server.py"
LOG="$ROOT/var/logs/pw/gui_server.out"
PORT="${FARA_PORT:-8766}"

echo "Stopping agent (if running)..."
curl -s -X POST "http://127.0.0.1:$PORT/agent/stop" 2>/dev/null || true
sleep 1

echo "Killing old gui_server process..."
pkill -f "gui_server.py --port $PORT" 2>/dev/null || true
sleep 1

echo "Starting containers..."
docker start fara-test fara-browser-pw >/dev/null 2>&1 || true

echo "Starting gui_server on :$PORT..."
mkdir -p "$(dirname "$LOG")"
cd "$ROOT/agent"
setsid nohup "$VENV" "$SCRIPT" --port "$PORT" > "$LOG" 2>&1 < /dev/null &
disown

sleep 2
if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/agent/status"; then
    echo "OK — gui_server up on http://127.0.0.1:$PORT"
else
    echo "WARNING — server non risponde, controlla $LOG"
fi
