"""Identical benchmark for any Fara model served on 127.0.0.1:8090.

Usage: model_bench.py <label>          -> writes bench/bench_models/<label>.json

Parts (all measured, nothing taken from the paper):
  A. raw speed: generation tok/s and image-prompt processing speed
  B. grounding: "click <description>" on live fixture pages; hit = the
     returned click lands inside the element's real DOM bounding box.
     Includes deliberately hard targets (16px scale circles, ARIA divs).
  C. judgement on REAL saved Prolific screenshots (study list choice)
  D. batch-fill decision quality/speed (text-only calls to the same model)
  E. DOM pipeline on the Qualtrics-like survey (answer sanity)
"""
import json, os, re, statistics as st, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pathlib import Path
from PIL import Image
import agent_core as ac, fara_prompt as fp, pw_bridge as pb, form_fill as base, form_fill_pw as ff

LABEL = sys.argv[1]
OUT = Path(__file__).resolve().parent.parent / "bench" / "bench_models"
OUT.mkdir(parents=True, exist_ok=True)
SHOTS = Path("/tmp/model_bench_shots"); SHOTS.mkdir(exist_ok=True)
R = {"label": LABEL}


def settle(): pb.wait_for_content(max_s=3, quiet_s=0.3); time.sleep(0.3)


# ---------------------------------------------------------------- A. speed
def part_a():
    def chat(content, max_tokens):
        p = {"model": "x", "messages": [{"role": "user", "content": content}], "temperature": 0.3,
             "max_tokens": max_tokens, "chat_template_kwargs": {"enable_thinking": False}}
        r = urllib.request.Request(ac.FARA_URL, data=json.dumps(p).encode(), headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(r, timeout=300))
    gen = []
    for _ in range(3):
        t = chat("Write a detailed 300-word explanation of how browser automation works.", 280).get("timings", {})
        gen.append(t.get("predicted_per_second", 0))
    R["A_gen_tok_s"] = round(st.median(gen), 1)
    pb.goto("file:///data/test_form.html"); settle()
    img = ac.take_screenshot(SHOTS, 1)
    times = []
    for i in range(3):
        d = Image.open(SHOTS / "screenshot_01.png").convert("RGB")
        d.putpixel((i, i), (i + 1, 2, 3))     # defeat the prompt cache: same cost as a new screenshot
        content = [{"type": "image_url", "image_url": {"url": ac.encode_for_model(d)}},
                   {"type": "text", "text": "Describe this page in one sentence."}]
        t = chat(content, 40).get("timings", {})
        times.append((t.get("prompt_ms", 0), t.get("prompt_n", 0)))
    R["A_image_prompt_ms"] = round(st.median([m for m, _ in times]))
    R["A_image_prompt_tokens"] = times[0][1]
    print("A", R["A_gen_tok_s"], "tok/s | image prompt", R["A_image_prompt_ms"], "ms /", R["A_image_prompt_tokens"], "tok", flush=True)


# ------------------------------------------------------------ B. grounding
def show_page(n):   # Qualtrics-like fixture: switch visible page
    pb.evaluate(f"(() => {{ const ps=[...document.querySelectorAll('.page')]; ps.forEach(p=>p.classList.remove('on')); ps[{n}].classList.add('on'); return 1; }})()")

TARGETS = [
    ("test.html", None, "the RED button", "#btn-red"),
    ("test.html", None, "the GREEN button", "#btn-green"),
    ("test.html", None, "the BLUE button", "#btn-blue"),
    ("test_form.html", None, "the 'Female' radio button", "input[name=gender][value=Female]"),
    ("test_form.html", None, "the 'Export' checkbox", "input[name=features][value=Export]"),
    ("test_form.html", None, "the country dropdown", "#q5_country"),
    ("test_form.html", None, "the comments text area", "#q4_comments"),
    ("test_form.html", None, "the height text input", "#q3_height"),
    ("test_form.html", None, "the 'Next' button", "#submit-btn"),
    ("test_newtab.html", None, "the 'Open study in new tab' link", "#open"),
    ("test_buttons_safety.html", None, "the 'Avanti' button", "button:has-text('Avanti')"),
    ("test_form_widgets.html", None, "the 'Cover B -- green' option", "[role=radio][data-value='Cover B']"),
    ("test_form_widgets.html", None, "the 'Wednesday' checkbox", "[role=checkbox][data-value='Wednesday']"),
    ("test_form_widgets.html", None, "the 'Germany' list option", "[role=option][data-value='Germany']"),
    ("test_form_widgets.html", None, "the 'Weekly' option", "label:has([id='QR~QID1~2'])"),
    ("test_qualtrics.html", 0, "the 'No' answer of the question about Mount Everest", "label:has([id='QR~QID2~2'])"),
    ("test_qualtrics.html", 0, "the blue '>>' next button", "#NextButton"),
    ("test_qualtrics.html", 1, "the leftmost circle of the scale (next to 'Not higher')", "label[for='QR~QID3~1']"),
    ("test_qualtrics.html", 1, "the rightmost circle of the scale (next to 'Much higher')", "label[for='QR~QID3~6']"),
    ("test_qualtrics.html", 1, "the third circle from the left of the scale", "label[for='QR~QID3~3']"),
    ("test_qualtrics.html", 1, "the fifth circle from the left of the scale", "label[for='QR~QID3~5']"),
    ("test_qualtrics.html", 2, "the 'Mt. Kilimanjaro' answer", "label:has([id='QR~QID4~2'])"),
    ("test_qualtrics.html", 3, "the 'Female' answer", "label:has([id='QR~QID5~2'])"),
    ("test_qualtrics.html", 3, "the age text box", "[id='QR~QID6']"),
]

def part_b():
    rows, lat = [], []
    cur = None
    for fx, page, desc, sel in TARGETS:
        if (fx, page) != cur:
            pb.goto(f"file:///data/{fx}"); settle()
            if page is not None: show_page(page); time.sleep(0.3)
            cur = (fx, page)
        box = pb.run(lambda b, s=sel: b.active_page().locator(s).first.bounding_box())
        if not box: rows.append({"t": desc, "err": "no bbox"}); continue
        ac.take_screenshot(SHOTS, 2)
        img = Image.open(SHOTS / "screenshot_02.png").convert("RGB")
        msgs = [{"role": "system", "content": fp.SYSTEM_PROMPT},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": ac.encode_for_model(img)}},
                                             {"type": "text", "text": f"Click on {desc}."}]}]
        t0 = time.time(); out = ac.call_fara(msgs); dt = time.time() - t0; lat.append(dt)
        try:
            th, act = ac.parse_thoughts_and_action(out); a = act["arguments"]
            x, y = fp.scale_coordinate(a["coordinate"]) if a.get("coordinate") else (None, None)
            kind = a.get("action")
        except Exception as e:
            rows.append({"t": desc, "err": str(e)[:60], "s": round(dt, 1)}); continue
        hit = x is not None and box["x"] <= x <= box["x"] + box["width"] and box["y"] <= y <= box["y"] + box["height"]
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        dist = None if x is None else round(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5)
        rows.append({"t": desc, "hit": bool(hit and kind in ("left_click", "double_click", "triple_click")),
                     "action": kind, "dist_px": dist, "size": f"{round(box['width'])}x{round(box['height'])}", "s": round(dt, 1)})
        print(f"B {'HIT ' if rows[-1]['hit'] else 'MISS'} {desc[:55]:55s} {kind:12s} dist={dist}px size={rows[-1]['size']} {dt:.1f}s", flush=True)
    R["B_rows"] = rows
    R["B_hits"] = sum(1 for r in rows if r.get("hit")); R["B_total"] = len(rows)
    R["B_latency_median_s"] = round(st.median(lat), 2)
    small = [r for r in rows if r.get("size") and min(map(int, r["size"].split("x"))) <= 24]
    R["B_small_hits"] = f"{sum(1 for r in small if r.get('hit'))}/{len(small)}"
    print("B total", R["B_hits"], "/", R["B_total"], "| small targets", R["B_small_hits"], "| median", R["B_latency_median_s"], "s", flush=True)


# ------------------------------------------------------- C. real screenshots
def part_c():
    preset = json.load(open(Path(__file__).parent / "presets.json"))[0]
    task = preset["context"] + "\n\n" + preset["task"] + ac.STANDING_RULES
    cases = [
        ("3 studies listed (1 interview, 1 Zoom meeting, 1 plain survey)", "run_20261008_131021/screenshot_02.png"),
        ("only the interview study listed", "run_20261008_110152/screenshot_01.png"),
    ]
    rows = []
    for name, rel in cases:
        img = Image.open(ac.LOG_ROOT / rel).convert("RGB")
        msgs = [{"role": "system", "content": fp.SYSTEM_PROMPT},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": ac.encode_for_model(img)}},
                                             {"type": "text", "text": task}]}]
        t0 = time.time(); out = ac.call_fara(msgs); dt = time.time() - t0
        th, act = ac.parse_thoughts_and_action(out); a = act["arguments"]
        co = fp.scale_coordinate(a["coordinate"]) if a.get("coordinate") else None
        # card 3 ("Everyday Thought") spans roughly x 275-728, y 650-833 in the 3-study screenshot
        if name.startswith("3"):
            if a.get("action") == "left_click" and co and 275 <= co[0] <= 728 and 650 <= co[1] <= 833: verdict = "BEST (opens the plain survey)"
            elif a.get("action") == "left_click" and co and co[1] < 410: verdict = "WRONG (opens the interview)"
            elif a.get("action") == "terminate": verdict = "WRONG (says nothing eligible)"
            else: verdict = f"neutral ({a.get('action')})"
        else:
            verdict = "OK (does not open it)" if a.get("action") != "left_click" else "WRONG (opens the interview)"
        rows.append({"case": name, "action": a.get("action"), "coord": co, "verdict": verdict, "s": round(dt, 1),
                     "thought": th.replace("\n", " ")[8:200]})
        print("C", verdict, "|", name, "|", a.get("action"), co, "|", th.replace("\n", " ")[8:140], flush=True)
    R["C_rows"] = rows


# ------------------------------------------------- D. batch-fill decisions
def part_d():
    view = json.load(open("/tmp/claude-1000/-home-riccardo-web-agent/02354944-ac9f-4922-bb4f-5094519ad9ae/scratchpad/fields15.json"))
    P = "A 29 year old software engineer from Milan."
    def quality(ans):
        iss = []
        for f in view:
            a = ans.get(f["id"])
            if a is None: iss.append("missing"); continue
            if f["type"] == "radio" or (f["type"] == "select" and not f.get("multiple")):
                if a not in f["options"]: iss.append("bad option")
            if f["type"] == "checkbox" or f.get("multiple"):
                if not isinstance(a, list) or any(x not in f["options"] for x in a): iss.append("bad multi")
            if f["type"] == "textarea" and "elaborate" in f["text"].lower():
                if not isinstance(a, str) or len(a) < 80: iss.append("weak text")
        return iss
    times, issues, und = [], 0, 0
    for _ in range(4):
        t = time.time(); ans, failed = base.ask_llm_for_answers(view, persona=P); times.append(time.time() - t)
        issues += len(quality(ans)); und += len(failed)
    R["D_15fields_median_s"] = round(st.median(times), 1); R["D_issues"] = issues; R["D_undecided"] = und
    # cross-page consistency + attention check (needs the browser)
    ok = 0; n = 3
    for _ in range(n):
        pb.goto("file:///data/test_consistency.html"); settle(); mem = []
        P2 = "Persona: a software engineer from Milan. The current year is 2026."
        ff.run_batch_fill(persona=P2, memory=mem); ff.click_advance_button(); ff.run_batch_fill(persona=P2, memory=mem)
        v = json.loads(pb.evaluate("""JSON.stringify({age:document.getElementById('age').value, exp:document.getElementById('exp').value,
            att:(document.querySelector('input[name=att]:checked')||{}).value, by:document.getElementById('byear').value, start:document.getElementById('start').value})"""))
        try: good = abs((2026 - int(v["by"])) - int(v["age"])) <= 1 and abs((2026 - int(v["start"])) - int(v["exp"])) <= 1 and v["att"] == "SD"
        except Exception: good = False
        ok += good
    R["D_consistency"] = f"{ok}/{n}"
    # Qualtrics-like survey through the DOM pipeline
    good = 0
    for _ in range(2):
        pb.goto("file:///data/test_qualtrics.html"); settle(); mem = []
        for _p in range(5):
            f = ff.extract_fields(); okk, _ = ff.batch_fill_verdict(f)
            if not okk: break
            r = ff.run_batch_fill(persona="A male software engineer from Milan, fluent in English. Answer factual questions correctly.", memory=mem)
            if r.get("mismatches") or r.get("apply_errors") or r.get("undecided"): break
            ff.click_advance_button()
        rec = json.loads(pb.evaluate("JSON.stringify(window.__answers)"))
        age = rec.get("QR~QID6", "")
        good += (len(rec) == 6 and rec.get("QR~QID3") in ("4", "5", "6") and rec.get("QR~QID4") == "1" and age.isdigit() and 18 <= int(age) <= 80)
    R["E_qualtrics_survey"] = f"{good}/2"
    print("D 15 fields median", R["D_15fields_median_s"], "s | issues", issues, "| undecided", und, "| consistency", R["D_consistency"], "| qualtrics", R["E_qualtrics_survey"], flush=True)


for name, fn in [("A", part_a), ("B", part_b), ("C", part_c), ("D", part_d)]:
    try: fn()
    except Exception as e:
        R[f"{name}_error"] = repr(e)[:300]; print(name, "ERROR", repr(e)[:300], flush=True)
json.dump(R, open(OUT / f"{LABEL}.json", "w"), indent=2, ensure_ascii=False)
print("saved", OUT / f"{LABEL}.json")
