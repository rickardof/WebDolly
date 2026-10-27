"""Functional suite for the Playwright layer: every result is verified
independently by reading the final DOM, never by trusting form_fill's own
report."""
import sys, time, json, os, urllib.request
import pw_bridge as pb, form_fill_pw as ff
import agent_core  # noqa: F401 -- importing injects the real-input (xdotool) backend into form_fill_pw

P = "A 29 year old software engineer from Milan."


def _model_up() -> bool:
    url = os.environ.get("FARA_URL", "http://127.0.0.1:8090/v1/chat/completions")
    try:
        urllib.request.urlopen(url.split("/v1/")[0] + "/health", timeout=2)
        return True
    except Exception:
        return False


# The run_batch_fill tests need the model server; the apply_answers / DOM tests
# do not. In CI (no model) only the latter run -- they are what exercises the
# real-input apply layer. With a model reachable, everything runs.
MODEL = _model_up()
if not MODEL:
    print("NOTE: model not reachable at FARA_URL -- running DOM/apply tests only, "
          "skipping run_batch_fill (LLM) tests\n")

def text_in_frames(sel):
    def f(b):
        for fr in b.active_page().frames:
            try:
                t = fr.locator(sel).first.text_content(timeout=500)
                if t: return t
            except Exception: pass
        return ""
    return pb.run(f)

def dom(js):
    return pb.evaluate(js)

results = []
def record(name, ok, detail=""):
    results.append(ok); print(f"{'PASS' if ok else 'FAIL'}  {name:46s} {detail}")

def fill_and_advance(fx, answers=None):
    pb.goto(f"file:///data/{fx}"); time.sleep(0.7)
    if answers is None:
        r = ff.run_batch_fill(persona=P)
    else:
        f = ff.extract_fields(); res = ff.apply_answers(f, answers)
        r = {"field_count": len(f), "apply_errors": res["errors"], "mismatches": [],
             "verified_state": ff.verify_fields(f)}
    return r, ff.click_advance_button()

# 1. trusted-events-only form must accept the submission (model: decides answers)
if MODEL:
    r, adv = fill_and_advance("test_trusted.html")
    record("trusted-only form accepts submission", text_in_frames("#res") == "TRUSTED-SUBMITTED", text_in_frames("#res"))
    # 2. iframe form fully filled + its own Next pressed
    r, adv = fill_and_advance("test_iframe.html")
    ok = r["field_count"] == 4 and not r["apply_errors"] and text_in_frames("#ok") == "IFRAME-SUBMITTED"
    record("iframe form filled and submitted", ok, f"fields={r['field_count']} errors={r['apply_errors']}")
# 3. Qualtrics-style hidden inputs + ARIA widgets: verify the actual DOM state
pb.goto("file:///data/test_form_widgets.html"); time.sleep(0.7)
f = ff.extract_fields()
ans = {"fs:0:radio": "Weekly", "fs:1:checkbox": ["API", "Reporting"], "rg:0": "Cover C -- red",
       "cgp:0": ["Monday", "Friday"], "lb:0": "Germany", "comments": "Ciao, perché è così?"}
res = ff.apply_answers(f, ans)
state = dom("""JSON.stringify({
  r: (document.querySelector('input[name="QR~QID1"]:checked')||{}).value,
  c: [...document.querySelectorAll('.qualtrics-like input[type=checkbox]:checked')].map(e=>e.value),
  aria_r: (document.querySelector('[role=radio][aria-checked=true]')||{}).textContent,
  aria_c: [...document.querySelectorAll('[role=checkbox][aria-checked=true]')].map(e=>e.dataset.value),
  lb: (document.querySelector('[role=option][aria-selected=true]')||{}).textContent,
  txt: document.getElementById('comments').value })""")
st = json.loads(state)
ok = (st["r"] == "Weekly" and sorted(st["c"]) == ["API", "Reporting"] and "Cover C" in (st["aria_r"] or "")
      and sorted(st["aria_c"]) == ["Friday", "Monday"] and st["lb"] == "Germany" and st["txt"] == ans["comments"])
record("hidden-native + ARIA widgets exact DOM state", ok, f"errors={res['errors']}" if not ok else "")
# 4. unchecking: re-apply with a different checkbox set must clear the others
res = ff.apply_answers(f, {"fs:1:checkbox": ["Dashboard"], "cgp:0": ["Wednesday"]})
st = json.loads(dom("""JSON.stringify({c:[...document.querySelectorAll('.qualtrics-like input[type=checkbox]:checked')].map(e=>e.value),
  a:[...document.querySelectorAll('[role=checkbox][aria-checked=true]')].map(e=>e.dataset.value)})"""))
record("re-apply clears previously checked options", st["c"] == ["Dashboard"] and st["a"] == ["Wednesday"], str(st))
# 5. 'Other: please specify' nested text box must not be clicked into by checkbox clicks
pb.goto("file:///data/test_form_complex.html"); time.sleep(0.7)
f = ff.extract_fields()
fid = next(x["id"] for x in f if x["type"] == "checkbox" and any(o.startswith("Other") for o in x["options"]))
other = next(o for o in next(x for x in f if x["id"] == fid)["options"] if o.startswith("Other"))
res = ff.apply_answers(f, {fid: ["Dashboard", other], "features_other_specify": "Webhooks"})
st = json.loads(dom("""JSON.stringify({c:[...document.querySelectorAll('input[name=features_used]:checked')].map(e=>e.value),
  t: document.getElementById('features_other_specify').value})"""))
record("'Other: specify' checkbox + its text box", sorted(st["c"]) == ["Dashboard", "Other"] and st["t"] == "Webhooks", str(st))
# 6. multi-page survey: page 1 advances, page 2 is then filled from scratch (model)
if MODEL:
    r, adv = fill_and_advance("test_form_complex.html")
    ok1 = adv.get("advanced") and "Page 2" in text_in_frames("#page2 h2")
    r2 = ff.run_batch_fill(persona=P); adv2 = ff.click_advance_button()
    ok2 = r2.get("field_count") == 4 and not r2.get("apply_errors") and text_in_frames("#done2") == "SUBMITTED"
    record("2-page survey: page 1 fill+advance", bool(ok1), f"clicked={adv.get('label')}")
    record("2-page survey: page 2 fill+submit", bool(ok2), f"{ff.summarize_for_agent(r2)[:70]}")
# 7. safety
pb.goto("file:///data/test_buttons_safety.html"); time.sleep(0.5)
ff.click_advance_button()
record("only the forward button is ever clicked", text_in_frames("#r") == "SAFE-NEXT", text_in_frames("#r"))
# 8. guardrails
for case, expect in [("login", False), ("consent", False), ("terms", False), ("survey4", True)]:
    pb.goto(f"file:///data/test_guardrails.html?c={case}"); time.sleep(0.5)
    ok, why = ff.batch_fill_verdict(ff.extract_fields())
    record(f"guardrail: {case}", ok == expect, why[:60])
# 9. a validation rejection must NOT be reported as 'advanced'
pb.goto("file:///data/test_trusted.html"); time.sleep(0.5)
adv = ff.click_advance_button()   # nothing filled -> page shows REJECTED
record("rejection is not reported as advanced", adv.get("advanced") is False, text_in_frames("#res")[:40])

# 10. Qualtrics-like paginated survey: 1-2 questions per page, ">>" next button,
#     unlabeled 6-point scale, text-entry question, missing personal fact (model).
if MODEL:
    pb.goto("file:///data/test_qualtrics.html"); time.sleep(0.7)
    pages = 0
    for _ in range(6):
        f = ff.extract_fields(); ok, _why = ff.batch_fill_verdict(f)
        if not ok: break
        r = ff.run_batch_fill(persona="A male software engineer from Milan, fluent in English. Answer factual questions correctly.")
        if r.get("mismatches") or r.get("apply_errors") or r.get("undecided"): break
        ff.click_advance_button(); pages += 1
    rec = json.loads(pb.evaluate("JSON.stringify(window.__answers)"))
    age = rec.get("QR~QID6", "")
    record("Qualtrics-like survey: all 4 pages via DOM", pages == 4 and len(rec) == 6, f"pages={pages} recorded={rec}")
    record("Qualtrics: scale answered toward 'Much higher'", rec.get("QR~QID3") in ("4", "5", "6"), f"scale={rec.get('QR~QID3')}")
    record("Qualtrics: age is a plausible invented number", age.isdigit() and 18 <= int(age) <= 80, f"age={age!r}")

print(f"\n{sum(results)}/{len(results)} passed   input paths: {ff.STATS}")
sys.exit(0 if all(results) else 1)
