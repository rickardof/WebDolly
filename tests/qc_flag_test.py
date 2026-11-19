"""Unit tests for noticing that a researcher's own checks rejected the work.

Runs standalone (no browser). Pure: page text in, verdict out.

The failure this file guards against is silent by construction. On 10 Nov
2026 an 80-trial image-comparison study ended on a page reading

    Your submission has been flagged ...
    ground_truth: Failed to correctly choose obvious test cases.

and every surface still called the cycle a clean success: the study handed
over a completion code, the code was banked, Prolific showed the submission
awaiting review. Four runs on that platform had ended the same way since
6 Nov without anything recording it. The verdict exists on exactly one
screen, for a few seconds, and nowhere else.

The cause of those four was a mechanical answer pattern -- the model
alternated left/right "to avoid straight-lining" without looking at the
images -- so the standing rules are checked here too: an A/B trial is to be
decided from what it shows, and no positional pattern, neither one side nor
an alternation, counts as an answer.
"""

import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "agent"))

for mod_name in ("playwright", "playwright.sync_api"):
    if mod_name not in sys.modules:
        m = types.ModuleType(mod_name)
        if mod_name == "playwright.sync_api":
            m.sync_playwright = lambda: None
        sys.modules[mod_name] = m

import agent_core as ac

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = ""):
    _RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name:66s} {detail}", flush=True)


def section(title: str):
    print(f"\n--- {title} " + "-" * max(0, 56 - len(title)), flush=True)


# The two pages the study actually served, as innerText reads them (the
# fingerprint collapses the newlines, which is why the detector works on one
# long line and returns a window rather than a line).
FLAGGED = (
    "Thank you for your participation! Total time: 26m 14s Your submission has been "
    "flagged. Our automatic checks have noticed some potential issues. This may be due "
    "to faulty checks on our side, or an issue with your voting style. We will still use "
    "your response, and apologise if you think you voted properly, nonetheless incase the "
    "checks were correct can you please make sure to read the instructions even more "
    "carefully in the future. Thank you for your time and effort! Validation Results: "
    "avg_response_time: Average response time is acceptable: 18.10s ground_truth: Failed "
    "to correctly choose obvious test cases. number_of_votes: All votes were submitted "
    "successfully. Return to Prolific and collect your reward"
)
PASSED = (
    "Thank you for your participation! Total time: 11m 20s Your submission has been "
    "validated. Thank you for your time and effort! Validation Results: avg_response_time: "
    "Average response time is acceptable: 17.86s ground_truth: Correctly chose obvious "
    "test cases. number_of_votes: All votes were submitted successfully. Return to "
    "Prolific and collect your reward"
)

section("qc_failure_in")

hit = ac.qc_failure_in(FLAGGED)
check("the page that rejected the work is recognised", bool(hit))
check("the researcher's own wording is kept, not just the word 'flagged'",
      "ground_truth" in hit or "Failed to correctly choose" in hit, hit[:90])
check("the snippet stays short enough for a Telegram line", len(hit) <= 220, str(len(hit)))
check("the page that accepted the work is left alone", ac.qc_failure_in(PASSED) == "")

# False positives would be worse than the bug: every study that warns about
# attention checks, or carries one, would be reported as a rejection.
for benign in (
    "Please note: this study contains attention checks. If you fail an attention check "
    "your submission may be rejected.",
    "This is an attention check. Please select Strongly disagree.",
    "To show you are paying attention, choose the third option.",
    "Your submission has been validated. Thank you!",
    "Failed to load the image. Please refresh.",
    "",
):
    check(f"not a rejection: {benign[:46]!r}", ac.qc_failure_in(benign) == "")

for real in (
    "You failed the attention check and cannot continue.",
    "Unfortunately you did not pass our quality checks.",
    "Your submission has been flagged for review.",
    "The comprehension check was not passed.",
):
    check(f"a rejection: {real[:46]!r}", bool(ac.qc_failure_in(real)))

section("from the page to summary.json")


def diag(step, **kw):
    d = {"type": "diag", "step": step, "url": "https://eval.services/?evaluation_id=x",
         "guarded": False, "timer_active": True}
    d.update(kw)
    return json.dumps(d)


lines = [diag(1), diag(2, qc_flag="ground_truth: Failed to correctly choose obvious test cases."),
         json.dumps({"step": 3, "action_type": "terminate", "args": {"answer": "CODE: CAAFDDC88"}}),
         diag(3, action="terminate", result="CODE: CAAFDDC88", code_source="url")]
s = ac.summarize_transcript(lines)
check("the verdict reaches the summary", s.get("flagged", "").startswith("ground_truth"))
check("a flagged run is still a completed study, because the code was real",
      s["outcome"] == "CODE" and s["code"] == "CAAFDDC88")

clean = ac.summarize_transcript([diag(1), diag(2),
                                 json.dumps({"step": 2, "action_type": "terminate",
                                             "args": {"answer": "CODE: CAAFDDC88"}})])
check("a run nobody flagged carries no flag field", "flagged" not in clean)

section("what the surfaces do with it")

gsrc = open(os.path.join(HERE, "..", "agent", "gui_server.py")).read()
check("the run record carries the verdict", '"flagged": s.get("flagged") or ""' in gsrc)
check("the dashboard row carries it as its last column",
      'r.get("gen_tps"), r.get("flagged") or ""' in gsrc)
check("the per-cycle ping says so instead of a bare tick", "_last_run_flag()" in gsrc)
check("a past rejection on Prolific's own pages is not read as this run's",
      '"" if (_qc_flag or here_guarded) else qc_failure_in' in
      open(os.path.join(HERE, "..", "agent", "agent_core.py")).read())
check("the daily summary lists the flagged runs",
      "Segnalati dal ricercatore" in gsrc.split("def _daily_summary_text")[1])

page = open(os.path.join(HERE, "..", "agent", "dashboard.html")).read()
check("the page reads the verdict from the column the server writes",
      "FLAG = 11;" in page and "r[FLAG]" in page)

section("the rule that stops it happening again")

for name, rules in (("full", ac.sites.current.standing_rules(compact=False)),
                    ("compact", ac.sites.current.standing_rules(compact=True))):
    low = rules.lower()
    check(f"{name}: planted ground-truth trials are named",
          "ground-truth" in low or "ground truth" in low)
    check(f"{name}: an alternation is refused like straight-lining",
          "alternation" in low or "left-right" in low)
    check(f"{name}: a past correction cannot excuse a planted check",
          "correction" in low and ("never covers" in low or "rule 1 wins" in low))
    check(f"{name}: the trial is still decided by looking at it",
          "both options" in low)

section("what batch-fill must not answer")

# The run that was rejected on 10 Nov filled five pages of an advertisement
# study with no model turn and no screenshot: 38 fields, decided from the DOM
# text alone. A study that shows an ad and then asks what the ad showed puts
# its manipulation check exactly there, and the text path cannot see the ad.
import form_fill as ff


def field(text, options=None, fid="fs:0:radio"):
    return {"id": fid, "text": text, "options": options or ["Yes", "No"]}


for q in (
    "Did the advertisement display a notice that AI was involved?",
    "Was there a logo in the image you just saw?",
    "How many people appear in the picture shown above?",
    "What colour was the car in the video?",
    "Does the advertisement contain an AI symbol?",
    "Nell'immagine era presente un simbolo?",
    "2. Was there a logo in the image you just saw?",
    "Do you remember what the video showed?",
):
    check(f"left to the eyes: {q[:46]!r}", ff.needs_eyes(field(q)))

# Ad studies write almost every opinion item in the past tense; those are not
# recall questions and must stay on the fast path, or a whole study is filled
# by hand one field at a time.
for q in (
    "The advertisement was believable",
    "The ad was annoying",
    "The advertisement showed the product clearly",
    "The ad contains humour",
    "How much did you like the advertisement?",
    "The advertisement above was informative",
    "I would consider buying the product in the ad.",
    "How much do you like this brand?",
    "Which of the following best describes your job?",
    "How many hours a week do you work?",
    "To show you are paying attention, please select 'Strongly disagree'.",
    "What is your age?",
    "I would consider buying this product.",
):
    check(f"batch-fill keeps: {q[:46]!r}", not ff.needs_eyes(field(q)))

# The agent-facing line has to name the question, not an id: the step that
# reads it is the one holding the screenshot.
msg = ff.summarize_for_agent({
    "skipped": False, "field_count": 8, "already_answered_count": 0,
    "undecided": ["fs:3:radio"], "mismatches": [], "apply_errors": {},
    "deferred_visual": [{"id": "fs:3:radio",
                         "text": "Did the advertisement display a notice that AI was involved?"}]})
check("the deferred question is named in words", "Did the advertisement display" in msg)
check("and is not repeated as a bare id", "fs:3:radio" not in msg, msg[-80:])

src = open(os.path.join(HERE, "..", "agent", "form_fill_pw.py")).read()
check("a deferred question is never sent to the text model",
      'unanswered = [f for f in unanswered if f["id"] not in skip]' in src)
check("and counts as undecided, which stops the blind auto-advance",
      '| {f["id"] for f in deferred_visual}' in src)

section("a question about a page the study has already left")

# run_20261113_114533, a University of Groningen yoghurt study. The scenario
# page said, in bold, three times: "a pleasurable eating experience",
# "prioritising enjoyment and pleasure". Six pages later the manipulation
# check asked what the family member's goal was, and batch-fill -- which
# reads the page in front of it and nothing else, on a page that also said
# "20g protein per pot" -- answered "Health and well-being". No model turn,
# no screenshot, study failed.
for q in (
    "Thinking back to the shopping scenario, the family member's main goal was primarily...",
    "Think back to the scenario you read earlier. What was the goal?",
    "Earlier in the study you were shown an image. What did it contain?",
    "According to the scenario, what was the price of the product?",
    "The advertisement you were shown earlier in this survey was for which brand?",
):
    check(f"left to the eyes: {q[:46]!r}", ff.needs_eyes(field(q)))

# ...and the cues still cannot drag an ordinary opinion page onto the slow
# path: without a stimulus word in the question there is nothing to refer
# back to, and an opinion about the thing in front of you is not a question
# about something earlier.
for q in (
    "Thinking about your own habits, how often do you buy yoghurt?",
    "Looking back over the past month, how satisfied were you with your job?",
    "According to your own experience, how healthy is this product?",
    "The advertisement was believable",
    "How much did you like the advertisement?",
):
    check(f"batch-fill keeps: {q[:46]!r}", not ff.needs_eyes(field(q)))

section("a question the DOM gave no text for")

# The same run: six pages, sixteen choice fields whose entire question text
# was "QID20-1", "QID22-1", ... and whose options were the raw input values
# ("on"). The text model answered every one of them and the page
# auto-advanced, so an attention check planted in any of them would be
# invisible -- to the model, to every guard, and to anyone reading the
# transcript afterwards.
check("a field whose text is just its own id is unreadable",
      ff.unreadable({"id": "QID20-1", "text": "QID20-1", "options": ["on"]}))
check("so is one with no text at all",
      ff.unreadable({"id": "QID22-1", "text": "", "options": ["on"]}))
for fb in ("fs:0:radio", "rg:2", "rgp:11", "cg:3", "sw:0", "lb:7", "bg:1"):
    check(f"so is the positional fallback {fb!r}",
          ff.unreadable({"id": fb, "text": fb, "options": ["a", "b"]}))
check("a real question is readable",
      not ff.unreadable({"id": "QID29", "text": "What is your gender?",
                         "options": ["Male", "Female"]}))
check("and so is a one-word label that is not the id",
      not ff.unreadable({"id": "age_input", "text": "Age", "options": []}))
check("a label that merely contains the id is still readable",
      not ff.unreadable({"id": "QID29", "text": "QID29. How often do you shop?",
                         "options": ["Daily"]}))

msg = ff.summarize_for_agent({
    "skipped": False, "field_count": 3, "already_answered_count": 0,
    "undecided": ["QID20-1", "QID22-1"], "mismatches": [], "apply_errors": {},
    "blind": ["QID20-1", "QID22-1"]})
check("an unreadable question is reported as left unanswered",
      "were NOT answered" in msg, msg)
check("...which says the page was not advanced", "The page was NOT advanced" in msg)
check("...and names what to look for", "attention check" in msg)
check("...and does not also list them as bare ids",
      "QID20-1" not in msg, msg[-90:])

ffpw = open(os.path.join(HERE, "..", "agent", "form_fill_pw.py")).read()
check("an unreadable question is never sent to the text model",
      'blind = [f for f in unanswered if base.unreadable(f) or f.get("needs_vision")]'
      in ffpw)
check("and counts as undecided, which stops the blind auto-advance",
      '| {f["id"] for f in blind})' in ffpw)
check("and travels back in the result", '"blind": [f["id"] for f in blind],' in ffpw)
check("options that cannot be told apart make a field unreadable",
      ff.unreadable({"id": "name:radio:QID20-1", "text": "Pick one",
                     "options": ["on", "on", "on"]}))
check("a namespaced id whose text is only the control name is unreadable",
      ff.unreadable({"id": "name:radio:QID29", "text": "QID29",
                     "options": ["Enjoyment", "Health"]}))
check("a Likert scale labelled 1..5 is still readable",
      not ff.unreadable({"id": "name:radio:QID7", "text": "I enjoy yoghurt",
                         "options": ["1", "2", "3", "4", "5"]}))

section("a dropdown the harness cannot open")

# run_20261115_180326, a Prolific-hosted task built with Element Plus: ten
# questions were <input class="el-input__inner" role="combobox" readonly
# aria-controls="el-id-5127-54">, with their options in a [role=listbox]
# teleported to <body> and hidden until the control is clicked. The extractor
# saw a text box with no options, the model wrote prose into it ("I generally
# enjoy meeting new people at social gatherings...") and every single one came
# back "Could NOT fill": a readonly input cannot be typed into. 35 of 60
# fields in that run failed that way.
check("a readonly input is never offered as a text field",
      "el.readOnly || el.getAttribute('role') === 'combobox'" in ffpw)
check("a combobox is paired with the listbox it owns",
      "_getComboboxes()" in ffpw and "aria-controls" in ff._SHARED_JS)
check("its options are read while the popup is still collapsed",
      "NOT filtered by _visible" in ff._SHARED_JS)
check("the field it produces is a choice, not free text",
      "widget: 'combobox', needs_vision: multi" in ffpw)
check("a tag-style multi-select is left to the vision step instead",
      'if f.get("multiple"):' in ffpw
      and 'base.unreadable(f) or f.get("needs_vision")' in ffpw)
check("the single-choice one is opened and clicked with the real cursor",
      'def _select_combobox(' in ffpw and '_REAL["key"]("Escape")' in ffpw)
check("the popup's own scroll box is scrolled when the option is past it",
      'def _scroll_inside_popup(' in ffpw and "_REAL[\"wheel\"](bool(pa[\"below\"]), 2)" in ffpw)
check("success is read off what the control shows, not off the click",
      "_COMBO_SHOWN_JS" in ffpw and "_norm_opt(str(shown)) == want" in ffpw)
check("the wait for the popup is bounded",
      "for _ in range(12):" in ffpw)
check("a listbox that a combobox owns is not emitted twice",
      "const owned = new Set(_getComboboxes().map(c => c.lb));" in ff._SHARED_JS)

# The two labels that walk produced on the same page, both scavenged from
# under <body>, where the poppers are teleported.
check("markup read as a question is unreadable",
      ff.unreadable({"id": "lb:3", "options": ["Yes", "No"],
                     "text": '<img height="1" width="1" style="display:none" '
                             'src="https://example.invalid/tr?id=1&ev=PageView"'}))
check("so is a question that is only its own options glued together",
      ff.unreadable({"id": "lb:4", "text": "None1-23-55-1010-20+20Not sure",
                     "options": ["None", "1-2", "3-5", "5-10", "10-20",
                                 "+20", "Not sure"]}))
check("a real question that happens to quote an option is still readable",
      not ff.unreadable({"id": "q7", "text": "Would you say Yes or No to that?",
                         "options": ["Yes", "No"]}))
check("and a question with a '<' in it is not mistaken for markup",
      not ff.unreadable({"id": "q8", "text": "Is your income < 20.000 EUR?",
                         "options": ["Yes", "No"]}))

section("what the study showed before this page")

pages = []
ac._remember_study_page(pages, "You are grocery shopping and need to buy a yoghurt "
                               "for a family member who wants a pleasurable eating experience.")
ac._remember_study_page(pages, "You are grocery shopping and need to buy a yoghurt "
                               "for a family member who wants a pleasurable eating experience.")
check("the same page seen twice is kept once", len(pages) == 1, str(len(pages)))
ac._remember_study_page(pages, "Please examine the yoghurt packaging carefully. "
                               "https://rug.eu.qualtrics.com/CP/Graphic.php?IM=IM_123")
check("a second, different page is kept", len(pages) == 2)
check("image URLs are not kept as if they were prose",
      "https://" not in pages[1], pages[1][-40:])
for i in range(8):
    ac._remember_study_page(pages, f"Instruction page number {i} with enough text to count "
                                   f"as a page of its own, number {i}.")
check("only the last few pages are kept", len(pages) == ac._STUDY_PAGES_KEPT, str(len(pages)))
check("a scrap of text is not a page", (ac._remember_study_page(pages, "Next"), len(pages))[1] == ac._STUDY_PAGES_KEPT)

acsrc = open(os.path.join(HERE, "..", "agent", "agent_core.py")).read()
check("every batch-filled question and answer reaches the transcript",
      '"batch_fill_qa"' in acsrc and '"qa": qa,' in acsrc)
check("a page with an unreadable question is never auto-advanced",
      'and not result.get("blind"))' in acsrc)
check("the recap is handed over only when something was deferred",
      'if (result.get("deferred_visual") or []) and study_pages:' in acsrc)
check("and the deferring step is told to answer from it, not from plausibility",
      "not from what sounds" in acsrc)
check("the pages are remembered off the fingerprint the step already computes",
      "_remember_study_page(_study_pages, curr_page_fp)" in acsrc)
check("they are never kept from Prolific's own pages",
      "if not here_guarded:\n                _remember_study_page" in acsrc)
check("and are dropped when the study ends",
      "_study_pages = []" in acsrc)

if __name__ == "__main__":
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    print(f"\n== SUMMARY ==  {passed}/{len(_RESULTS)} passed")
    sys.exit(0 if passed == len(_RESULTS) else 1)
