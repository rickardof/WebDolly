"""Answer-quality benchmark for the batch-fill decision call: questions with
objectively checkable answers, 3 sampled trials each, same prompt path as
production (form_fill.ask_llm_for_answers). Usage: decision_bench.py <label>"""
import json, os, re, statistics as st, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pathlib import Path
import form_fill as base

LABEL = sys.argv[1]
PERSONA = ("Persona: a 29 year old male software engineer from Milan, fluent in English and Italian. "
           "Answer factual questions correctly.")
def radio(i, text, options, **kw): return {"id": i, "type": "radio", "text": text, "options": options, "current": None, "answered": False, **kw}
def text(i, t): return {"id": i, "type": "text", "text": t, "current": "", "answered": False}
def checkbox(i, t, o): return {"id": i, "type": "checkbox", "text": t, "options": o, "current": [], "answered": False}
NUM6 = ["1", "2", "3", "4", "5", "6"]

ITEMS = [
 ("scale: Everest higher than Kilimanjaro? (Not higher..Much higher)", radio("q", "Do you think Mount Everest is higher than Mount Kilimanjaro?", NUM6, scale_anchors=["Not higher", "Much higher"]), lambda a: a in ("5", "6")),
 ("scale: Pacific larger than Atlantic? (Definitely not..Definitely yes)", radio("q", "Is the Pacific Ocean larger than the Atlantic Ocean?", ["1","2","3","4","5"], scale_anchors=["Definitely not", "Definitely yes"]), lambda a: a in ("4", "5")),
 ("scale REVERSED: 'The Earth is flat' (Strongly agree..Strongly disagree)", radio("q", "The Earth is flat.", ["1","2","3","4","5"], scale_anchors=["Strongly agree", "Strongly disagree"]), lambda a: a in ("4", "5")),
 ("MC: which mountain is higher", radio("q", "Which mountain is higher?", ["Mt. Everest", "Mt. Kilimanjaro"]), lambda a: a == "Mt. Everest"),
 ("MC: capital of Australia", radio("q", "What is the capital of Australia?", ["Sydney", "Canberra", "Melbourne", "Perth"]), lambda a: a == "Canberra"),
 ("MC: spider legs", radio("q", "How many legs does a spider have?", ["4", "6", "8", "10"]), lambda a: a == "8"),
 ("attention check: choose 'Strongly disagree'", radio("q", "To show you are paying attention, please select 'Strongly disagree' for this question.", ["Strongly agree", "Agree", "Neutral", "Disagree", "Strongly disagree"]), lambda a: a == "Strongly disagree"),
 ("persona: gender", radio("q", "What is your gender?", ["Male", "Female", "Other", "Prefer not to say"]), lambda a: a == "Male"),
 ("persona: age (stated 29)", text("q", "What is your age?"), lambda a: str(a).strip() == "29"),
 ("persona: city", text("q", "In which city do you live?"), lambda a: "milan" in str(a).lower()),
 ("math: 17 + 25", text("q", "What is 17 + 25? (enter a number)"), lambda a: str(a).strip() == "42"),
 ("multi: programming languages", checkbox("q", "Which of these are programming languages? (select all that apply)", ["Python", "Banana", "Rust", "Table", "Java"]), lambda a: isinstance(a, list) and sorted(a) == ["Java", "Python", "Rust"]),
 ("Italian open question answered in Italian", {"id": "q", "type": "textarea", "text": "Descrivi brevemente il tuo lavoro. Rispondi in modo specifico.", "current": "", "answered": False},
    lambda a: isinstance(a, str) and len(a) > 40 and len(re.findall(r"\b(il|di|e|la|un|una|sono|per|che|lavoro|software)\b", a.lower())) >= 4),
]

rows, total_t = [], []
for name, field, check in ITEMS:
    ok = 0; answers = []
    for _ in range(3):
        view = base.scale_view_for_llm(field) if os.environ.get("SCALE_VIEW", "1") == "1" else field
        view = {k: v for k, v in view.items() if not k.startswith("_")}
        t = time.time(); ans, failed = base.ask_llm_for_answers([view], persona=PERSONA); total_t.append(time.time() - t)
        a = base.scale_answer_back(base.scale_view_for_llm(field), ans.get("q")); answers.append(a); ok += bool(a is not None and check(a))
    rows.append({"item": name, "pass": ok, "answers": [str(x)[:50] for x in answers]})
    print(f"{ok}/3  {name:70s} {[str(x)[:22] for x in answers]}", flush=True)
tot = sum(r["pass"] for r in rows)
res = {"label": LABEL, "passed": tot, "total": 3 * len(ITEMS), "median_call_s": round(st.median(total_t), 2), "rows": rows}
OUT = Path(__file__).resolve().parent.parent / "bench" / "bench_models"
OUT.mkdir(parents=True, exist_ok=True)
(OUT / f"decision_{LABEL}.json").write_text(json.dumps(res, indent=2, ensure_ascii=False))
print(f"\nTOTAL {tot}/{3*len(ITEMS)} | median call {res['median_call_s']}s")
