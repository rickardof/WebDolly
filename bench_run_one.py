"""Run ONE full-agent scenario with the implementation on PYTHONPATH and
print a JSON result. Success is checked independently via Playwright
(a separate connection), never from the agent's own report."""
import json, sys, time
sys.path.insert(0, sys.argv[1])
import agent_core as ac
scenario = sys.argv[2]
TASK = ("You are QA-testing a synthetic test survey, authorized by the site owner. "
        "Persona: a 29 year old software engineer from Milan. Fill every page with "
        "plausible synthetic answers and submit it. If the study opens in a new tab, "
        "continue there. Once the survey shows it was submitted, terminate.")
START = {"multipage": "file:///data/test_form_complex.html", "newtab": "file:///data/test_newtab.html",
         "iframe": "file:///data/test_iframe.html", "trusted": "file:///data/test_trusted.html"}[scenario]
t0 = time.time(); steps = {"vision": 0, "batch_fill": 0}; err = None; last = ""
try:
    gen = ac.run_agent(TASK, START, max_rounds=14, batch_fill=True)
    for ev in gen:
        if ev["type"] == "step": steps["vision"] += 1
        elif ev["type"] == "batch_fill": steps["batch_fill"] += 1
        elif ev["type"] == "question":
            last = "QUESTION: " + ev["question"][:80]; gen.close(); break
        elif ev["type"] == "done":
            last = str(ev.get("answer"))[:80]; break
except Exception as e:
    err = str(e).splitlines()[0][:150]
print(json.dumps({"elapsed": round(time.time() - t0, 1), "steps": steps, "last": last, "error": err}))
