import time, statistics as st, subprocess, sys
import form_fill as old, form_fill_pw as new, pw_bridge as pb
P = "A 29 year old software engineer from Milan."
def nav_old(u): subprocess.run(["docker","exec","fara-browser-pw","cdp.py","navigate",u],check=True,capture_output=True); time.sleep(1)
def nav_new(u): pb.goto(u); time.sleep(1)
def T(fn):
    t = time.perf_counter(); r = fn(); return r, (time.perf_counter()-t)*1000
for fx in ["test_form_complex.html", "test_form_widgets.html"]:
    url = f"file:///data/{fx}"
    nav_new(url); fields = new.extract_fields()
    answers, _ = new.ask_llm_for_answers([{k:v for k,v in f.items() if not k.startswith('_') and k!='widget'} for f in fields], persona=P)
    res = {s: {p: [] for p in ["extract","apply","verify","advance"]} for s in ("old","new")}
    for rep in range(5):
        nav_old(url)
        _,t = T(old.extract_fields); res["old"]["extract"].append(t)
        _,t = T(lambda: old.apply_answers(answers)); res["old"]["apply"].append(t)
        _,t = T(lambda: old.verify_fields(list(answers))); res["old"]["verify"].append(t)
        _,t = T(old.click_advance_button); res["old"]["advance"].append(t)
        nav_new(url)
        f,t = T(new.extract_fields); res["new"]["extract"].append(t)
        _,t = T(lambda: new.apply_answers(f, answers)); res["new"]["apply"].append(t)
        _,t = T(lambda: new.verify_fields(f)); res["new"]["verify"].append(t)
        _,t = T(new.click_advance_button); res["new"]["advance"].append(t)
    print(f"\n== {fx} ({len(answers)} answers, median of 5, ms) ==")
    print(f"{'phase':9s} {'old cdp.py':>11s} {'playwright':>11s}")
    tot = {"old":0,"new":0}
    for ph in ["extract","apply","verify","advance"]:
        o, n = st.median(res["old"][ph]), st.median(res["new"][ph]); tot["old"]+=o; tot["new"]+=n
        print(f"{ph:9s} {o:11.0f} {n:11.0f}")
    print(f"{'TOTAL':9s} {tot['old']:11.0f} {tot['new']:11.0f}")
print("input paths:", new.STATS)
