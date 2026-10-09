#!/bin/bash
# Launches the Playwright version of the Fara control panel (port 8766,
# isolated browser container fara-browser-pw). The original launcher,
# start_gui.sh (port 8765), is left untouched.
set -e

LOG=/home/riccardo/web-agent/logs_pw/start_gui.out
mkdir -p /home/riccardo/web-agent/logs_pw
exec >"$LOG" 2>&1

docker start fara-test fara-browser-pw >/dev/null 2>&1 || true

if ! curl -s -o /dev/null --max-time 2 http://127.0.0.1:8766/agent/status; then
    cd /home/riccardo/web-agent/controller_pw
    setsid nohup /home/riccardo/web-agent/venv_pw/bin/python gui_server.py --port 8766 \
        > /home/riccardo/web-agent/logs_pw/gui_server.out 2>&1 < /dev/null &
    disown
    sleep 3
fi

if command -v google-chrome >/dev/null; then
    setsid google-chrome --new-window http://127.0.0.1:8766 < /dev/null > /dev/null 2>&1 &
elif command -v firefox >/dev/null; then
    setsid firefox --new-window http://127.0.0.1:8766 < /dev/null > /dev/null 2>&1 &
else
    setsid xdg-open http://127.0.0.1:8766 < /dev/null > /dev/null 2>&1 &
fi
disown
