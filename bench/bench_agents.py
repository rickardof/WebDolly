"""Orchestrate old-vs-Playwright full-agent runs, sequentially, with a
clean browser state before each and an independent success check after."""
import json, os, subprocess, sys, time
from pathlib import Path
W = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, f"{W}/agent")
import pw_bridge as pb
IMPL = {"old": ("python3", f"{W}/controller_oldbench"),
        "new": (f"{W}/venv_pw/bin/python", f"{W}/agent")}
CHECK = {"multipage": ("#done2", "SUBMITTED"), "newtab": ("#done", "SUBMITTED"),
         "iframe": ("#ok", "IFRAME-SUBMITTED"), "trusted": ("#res", "TRUSTED-SUBMITTED")}
def reset():
    pb.run(lambda b: [p.close() for p in b.pages()[1:]])
    pb.goto("about:blank"); time.sleep(0.5)
def success(scn):
    sel, want = CHECK[scn]
    def f(b):
        for p in b.pages():
            for fr in p.frames:
                try:
                    t = fr.locator(sel).first.text_content(timeout=300)
                    if t and want in t: return t
                except Exception: pass
        return ""
    return pb.run(f)
reps = int(sys.argv[1]) if len(sys.argv) > 1 else 1
scns = sys.argv[2].split(",") if len(sys.argv) > 2 else list(CHECK)
for rep in range(reps):
    for scn in scns:
        for impl in os.environ.get("BENCH_IMPLS", "old,new").split(","):
            reset()
            py, path = IMPL[impl]
            out = subprocess.run([py, f"{W}/bench_run_one.py", path, scn], capture_output=True, text=True, timeout=900)
            line = (out.stdout.strip().splitlines() or ["{}"])[-1]
            try: r = json.loads(line)
            except Exception: r = {"error": (out.stderr or out.stdout)[-200:]}
            ok = bool(success(scn))
            r.update({"rep": rep, "scenario": scn, "impl": impl, "success": ok})
            print(json.dumps(r), flush=True)
