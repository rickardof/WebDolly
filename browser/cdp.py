#!/usr/bin/env python3
"""Minimal CDP client used from inside the browser container (loopback-only
DevTools port). Run via `docker exec fara-browser cdp.py <cmd> [arg]`.
"""

import json
import sys
import urllib.request

import websocket

CDP_HTTP = "http://127.0.0.1:9222"


def get_page_ws_url():
    with urllib.request.urlopen(f"{CDP_HTTP}/json") as r:
        targets = json.load(r)
    for t in targets:
        if t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("no page target found")


def send(ws, method, params=None, msg_id=1):
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        resp = json.loads(ws.recv())
        if resp.get("id") == msg_id:
            return resp


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: cdp.py <navigate URL | eval JS_EXPR>")
    cmd = sys.argv[1]
    ws_url = get_page_ws_url()
    ws = websocket.create_connection(ws_url, timeout=10)
    try:
        if cmd == "navigate":
            resp = send(ws, "Page.navigate", {"url": sys.argv[2]})
        elif cmd == "eval":
            resp = send(ws, "Runtime.evaluate", {"expression": sys.argv[2]})
        else:
            raise SystemExit(f"unknown cmd {cmd!r}")
        print(json.dumps(resp))
    finally:
        ws.close()


if __name__ == "__main__":
    main()
