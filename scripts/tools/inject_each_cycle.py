"""Re-inject a standing instruction into a running agent at the start of
every loop cycle (each cycle starts a fresh conversation, so a one-off
injection only lasts one cycle). Talks only to the GUI's HTTP API; exits
by itself when the run stops."""
import json, sys, time, urllib.request
BASE = sys.argv[1]; NOTE = sys.argv[2]
def get():
    return json.load(urllib.request.urlopen(BASE + "/agent/status", timeout=10))
def inject():
    req = urllib.request.Request(BASE + "/agent/inject", data=json.dumps({"text": NOTE}).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=10).read().decode()
seen = -1
while True:
    try:
        s = get()
    except Exception:
        time.sleep(3); continue
    if not s["running"]:
        print("run stopped -> watcher exits", flush=True); break
    cycles = sum(1 for e in s["events"] if e["type"] == "cycle") or 1
    if cycles != seen:
        print(f"cycle {cycles}: inject -> {inject()}", flush=True); seen = cycles
    time.sleep(2)
