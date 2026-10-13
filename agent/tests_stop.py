"""Measures how long /agent/stop takes to actually stop a run, in the three
situations where the agent is blocked on something slow."""
import json, sys, time, urllib.request
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766"
def api(path, body=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=15))
SCEN = {
  "decisioni batch-fill": dict(url="file:///data/test_form_complex.html", batch_fill=True, stop_at=5,
      task="Fill the form with plausible synthetic answers and submit it."),
  "passo visivo":         dict(url="file:///data/test.html", batch_fill=False, stop_at=1.5,
      task="Click the RED button, then the GREEN button, then the BLUE button, one per step, then terminate."),
  "azione wait":          dict(url="file:///data/test.html", batch_fill=False, stop_at=8,
      task="Use the wait action with time 30 seconds. Then terminate."),
}
worst = 0
for name, sc in SCEN.items():
    while api("/agent/status")["running"]: time.sleep(1)
    api("/start_agent", dict(task=sc["task"], url=sc["url"], max_rounds=10, loop=False, poll_interval=5, batch_fill=sc["batch_fill"]))
    time.sleep(sc["stop_at"])
    if not api("/agent/status")["running"]:
        print(f"{name:22s} run already finished before stop -> skipped"); continue
    t = time.time(); api("/agent/stop", {})
    while api("/agent/status")["running"] and time.time() - t < 90: time.sleep(0.1)
    dt = time.time() - t; worst = max(worst, dt)
    print(f"{name:22s} stop -> fermo in {dt:5.1f}s")
print(f"WORST: {worst:.1f}s")
