"""Unit tests for how a cycle's outcome is counted as a completed study.

Runs standalone (no browser). Everything here is pure: a terminate answer
in, a verdict out, plus the pass that tells a re-reported completion from a
new one.

The three bugs this file guards against, all of them silent -- the numbers
simply read higher than the work actually done (103 "studi completati"
against 64 real ones over a month of runs):

  * "CODE: NOCODE": the model is told to answer NOCODE when a study has no
    completion code, and 15 cycles put that word in the code slot instead.
    Trusting the "CODE:" prefix filed them as submissions carrying a
    copyable code that read NOCODE.
  * NOCODE counted as a completed study: a cycle that ends without a code
    banked nothing, and some of those ran 2 steps or 4 hours with no study
    at all.
  * the same completion counted twice: a cycle that starts while the browser
    still sits on the previous study's submissions/complete?cc= URL reads
    that code again and ends in seconds.
"""

import json
import os
os.environ.setdefault("FARA_SITE", "prolific")  # this suite exercises the Prolific site profile
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "agent"))

# agent_core/gui_server pull in Playwright at import time; none of the code
# under test touches a browser.
for mod_name in ("playwright", "playwright.sync_api"):
    if mod_name not in sys.modules:
        m = types.ModuleType(mod_name)
        if mod_name == "playwright.sync_api":
            m.sync_playwright = lambda: None
        sys.modules[mod_name] = m

import agent_core as ac
import gui_server as gs

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = ""):
    _RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name:62s} {detail}", flush=True)


def section(title: str):
    print(f"\n--- {title} " + "-" * max(0, 56 - len(title)), flush=True)


def rec(run: str, outcome: str, answer: str, studies=None, code=None, source="url"):
    """A dashboard record, as _run_record builds it."""
    from datetime import datetime
    return {"started": datetime.strptime(run, "%Y%m%d_%H%M%S"),
            "ended": datetime.strptime(run, "%Y%m%d_%H%M%S"),
            "duration_s": 60, "steps": 10, "outcome": outcome, "answer": answer,
            "code": code if code is not None else ac.completion_code_of(answer),
            "studies": list(studies or []), "code_source": source,
            "where": {"list": 1, "study": 9, "elsewhere": 0}, "gen_tps": None}


# ----------------------------------------------- what counts as a real code
section("completion_code_of")

check("a real code is a code", ac.completion_code_of("CODE: C1NKBGUZ") == "C1NKBGUZ")
check("surrounding whitespace is ignored", ac.completion_code_of("  CODE:  CBBD09819 ") == "CBBD09819")
for bad in ("CODE: NOCODE", "CODE: nocode", "CODE: NO-CODE", "CODE:", "CODE:   ",
            "CODE: none", "CODE: N/A", "CODE: na", "CODE: nessuno", "CODE: --"):
    check(f"{bad!r} is not a completion", ac.completion_code_of(bad) == "")
check("a code that merely starts with a no-code word survives",
      ac.completion_code_of("CODE: NONE123") == "NONE123")
check("a plain NOCODE answer carries no code", ac.completion_code_of("NOCODE") == "")
check("so does an empty answer", ac.completion_code_of("") == "")
check("a non-answer is not mistaken for one", ac.completion_code_of("max_rounds=1500") == "")

# ------------------------------------------------------ summarize_transcript
section("summarize_transcript")

def _lines(answer: str, sid: str = "6ae222ed95e73256bb94f587"):
    L = "https://app.prolific.com/studies"
    return [json.dumps(x) for x in [
        {"type": "diag", "step": 1, "url": f"{L}/{sid}", "guarded": True, "timer_active": False},
        {"type": "diag", "step": 2, "url": "https://q.example.com/s", "guarded": False,
         "timer_active": True},
        {"step": 3, "action_type": "terminate", "args": {"answer": answer}, "code_source": "url"},
    ]]

s = ac.summarize_transcript(_lines("CODE: C1NKBGUZ"))
check("a real code reads as a completion", s["outcome"] == "CODE", s["outcome"])
check("and the code itself is kept, so nobody re-parses the answer",
      s["code"] == "C1NKBGUZ", s["code"])
s = ac.summarize_transcript(_lines("CODE: NOCODE"))
check("'CODE: NOCODE' reads as NOCODE, not as a completion", s["outcome"] == "NOCODE", s["outcome"])
check("and it carries no code", s["code"] == "", repr(s["code"]))
check("a plain NOCODE still reads as NOCODE",
      ac.summarize_transcript(_lines("NOCODE"))["outcome"] == "NOCODE")
check("an empty list still reads as NONE",
      ac.summarize_transcript(_lines("NONE"))["outcome"] == "NONE")
check("a user stop still reads as STOPPED",
      ac.summarize_transcript(_lines("STOPPED"))["outcome"] == "STOPPED")
check("anything else the model says is a plain terminate",
      ac.summarize_transcript(_lines("I give up"))["outcome"] == "TERMINATE")
check("an unfinished run reads as INCOMPLETE",
      ac.summarize_transcript(_lines("x")[:2])["outcome"] == "INCOMPLETE")
check("every summary carries the classifier version that wrote it",
      s["v"] == ac.SUMMARY_V == 2, s["v"])

check("the NOCODE guard and the counter share one definition of 'no code'",
      all(ac._is_nocode_answer(a) for a in
          ("NOCODE", "CODE: NOCODE", "CODE: N/A", "CODE:", "CODE: nessuno"))
      and not any(ac._is_nocode_answer(a) for a in
                  ("CODE: C1NKBGUZ", "NONE", "", "I give up")))

# ------------------------------------------- correcting summaries on disk
section("_reclassify")

old = {"steps": 124, "outcome": "CODE", "answer": "CODE: NOCODE", "code_source": "model"}
r = gs._reclassify(dict(old))
check("a summary written by the old classifier is corrected on read",
      r["outcome"] == "NOCODE" and r["code"] == "", f"{r['outcome']} {r['code']!r}")
r = gs._reclassify({"outcome": "CODE", "answer": "CODE: CBBD09819"})
check("a real completion keeps its verdict and gains its code",
      r["outcome"] == "CODE" and r["code"] == "CBBD09819")
r = gs._reclassify({"outcome": "NONE", "answer": "NONE"})
check("an outcome this version does not touch is left alone", r["outcome"] == "NONE")
r = gs._reclassify({"v": ac.SUMMARY_V, "outcome": "CODE", "answer": "CODE: NOCODE", "code": "x"})
check("a current summary is not second-guessed",
      r["outcome"] == "CODE" and r["code"] == "x")
check("reclassifying twice gives the same answer",
      gs._reclassify(gs._reclassify(dict(old)))["outcome"] == "NOCODE")

# ------------------------------------------------- re-reported completions
section("_mark_repeats")

A, B = "6af1f98425603cfc46274e68", "6ae33be836340c811c073b6f"
runs = [
    rec("20261109_175856", "CODE", "CODE: CBBD09819", [A]),        # earned it
    rec("20261109_183807", "CODE", "CODE: CBBD09819", []),         # read it again
]
gs._mark_repeats(runs)
check("the cycle that earned the code keeps it", gs._outcome_of(runs[0]) == "CODE")
check("the cycle that re-read it off the stale URL does not",
      gs._outcome_of(runs[1]) == "REPEAT")
check("and the stored outcome is left intact, so the pass can be redone",
      runs[1]["outcome"] == "CODE")
gs._mark_repeats(runs); gs._mark_repeats(runs)
check("marking repeats again gives the same answer",
      [gs._outcome_of(r) for r in runs] == ["CODE", "REPEAT"])

# One researcher's recurring series hands out the same code for different
# study ids: C3YV0TJP came back under four of them in a month.
series = [rec("20261021_181444", "CODE", "CODE: C3YV0TJP", ["6aaba16a86719e1eaad46908"]),
          rec("20261022_193912", "CODE", "CODE: C3YV0TJP", ["6aaba16df29a53acca7ab693"]),
          rec("20261029_180837", "CODE", "CODE: C3YV0TJP", [])]
gs._mark_repeats(series)
check("the same code under a second study id is a second completion",
      [gs._outcome_of(r) for r in series] == ["CODE", "CODE", "REPEAT"],
      str([gs._outcome_of(r) for r in series]))

twice = [rec("20261028_173303", "CODE", "CODE: C1NQYU5R", [A]),
         rec("20261028_174601", "CODE", "CODE: 8HLDJ", [A])]
gs._mark_repeats(twice)
check("a study taken twice is one submission, whatever the second code says",
      [gs._outcome_of(r) for r in twice] == ["CODE", "REPEAT"])

# A cycle that opens a study it already did, skips it and completes another
# must be judged on the one it finished.
browsed = [rec("20261030_101837", "CODE", "CODE: C1NKBGUZ", [A]),
           rec("20261030_104500", "CODE", "CODE: CZCNFN38", [A, B])]
gs._mark_repeats(browsed)
check("looking at a finished study before completing a new one is not a repeat",
      [gs._outcome_of(r) for r in browsed] == ["CODE", "CODE"],
      str([gs._outcome_of(r) for r in browsed]))

noncode = [rec("20261109_102733", "NOCODE", "NOCODE", [A]),
           rec("20261109_110055", "NOCODE", "NOCODE", [A]),
           rec("20261109_120846", "NONE", "NONE", []),
           rec("20261109_130000", "INCOMPLETE", "", [A])]
gs._mark_repeats(noncode)
check("cycles that banked nothing are never flagged as repeats",
      [gs._outcome_of(r) for r in noncode] == ["NOCODE", "NOCODE", "NONE", "INCOMPLETE"])
check("a user stop is reported as a stop, not as an internal name",
      gs._outcome_of(rec("20261109_140000", "STOPPED", "STOPPED")) == "TERMINATE")

# ------------------------------------------------- what the page then shows
section("dashboard page")

page = open(os.path.join(HERE, "..", "agent", "dashboard.html")).read()
check("only CODE counts as a completed study on the page",
      "const kind = o => o === 'CODE' ? 'done' : o === 'NONE' ? 'empty' : 'fail';" in page)
check("a re-read code and a code-less end are named in plain words",
      "REPEAT: 'codice già registrato'" in page and "NOCODE: 'finito senza codice'" in page)
check("a plain terminate still shows the model's own words",
      "TERMINATE:" not in page.split("const OUT_LABEL")[1].split("};")[0])

# The charts and the aggregation count into hard-coded {done, fail, empty}
# objects, so a fourth bucket out of kind() would quietly produce NaN columns
# rather than an error.
import re as _re
kind_src = _re.search(r"const kind = o =>.*", page).group(0)
check("kind() still yields only the three buckets the page can count",
      set(_re.findall(r"'([a-z]+)'", kind_src)) == {"done", "fail", "empty"},
      kind_src.strip())

# The daily Telegram line and the page must not be able to drift apart.
gsrc = open(os.path.join(HERE, "..", "agent", "gui_server.py")).read()
check("the daily summary counts with the same two helpers",
      "_mark_repeats(" in gsrc.split("def _daily_summary_text")[1]
      and "_outcome_of(r)" in gsrc.split("def _daily_summary_text")[1])
check("the per-cycle Telegram ping no longer trusts the 'CODE:' prefix",
      "if agent_core.completion_code_of(_ca):" in gsrc)


if __name__ == "__main__":
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    print(f"\n== SUMMARY ==  {passed}/{len(_RESULTS)} passed")
    sys.exit(0 if passed == len(_RESULTS) else 1)
