#!/bin/bash
# Avvia lo stack completo: container + GUI, e apre il pannello nel browser.
# I percorsi vengono dalla posizione di questo script.
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${FARA_PORT:-8766}"
LOG="$ROOT/var/logs/pw/start_gui.out"
mkdir -p "$(dirname "$LOG")"
exec >"$LOG" 2>&1

docker start fara-test fara-browser-pw >/dev/null 2>&1 || true

if ! curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/agent/status"; then
    cd "$ROOT/agent"
    setsid nohup "$ROOT/venv_pw/bin/python" gui_server.py --port "$PORT" \
        > "$ROOT/var/logs/pw/gui_server.out" 2>&1 < /dev/null &
    disown
    sleep 3
fi

if command -v google-chrome >/dev/null; then
    setsid google-chrome --new-window "http://127.0.0.1:$PORT" < /dev/null > /dev/null 2>&1 &
elif command -v firefox >/dev/null; then
    setsid firefox --new-window "http://127.0.0.1:$PORT" < /dev/null > /dev/null 2>&1 &
else
    setsid xdg-open "http://127.0.0.1:$PORT" < /dev/null > /dev/null 2>&1 &
fi
disown
