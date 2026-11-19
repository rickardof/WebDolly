#!/usr/bin/env python3
"""Site profiles: how one is chosen, and what the generic one switches off.

The agent loop is generic; everything it knows about one platform lives in a
profile under agent/sites/. These tests pin down the contract that makes that
true:

  * a profile is chosen explicitly (FARA_SITE, the run's own setting) or from
    the start URL / the page the browser is on, else the last one used, else
    generic;
  * with the generic profile no platform rule fires on ANY url -- not even on
    a platform's own -- and no platform text reaches the model;
  * a whole run on an ordinary site, driven through run_agent with the browser
    and the model stubbed, starts no task timer and sends no platform rules,
    while the same run with the Prolific profile does (so the test can tell).

Offline: no browser, no model. Run from anywhere:
    python tests/site_profiles_test.py
"""
import inspect
import json
import os
import sys
import tempfile
import types

os.environ.pop("FARA_SITE", None)           # this suite chooses profiles itself
# Never reach a real browser: nothing listens on port 9, so a stray bridge
# call fails instead of touching the live session on :9333.
os.environ["FARA_CDP_URL"] = "http://127.0.0.1:9"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "agent"))
_TMP = tempfile.mkdtemp()
os.environ["FARA_LAST_SITE_FILE"] = os.path.join(_TMP, "last_site")
os.environ.setdefault("FARA_LOG_ROOT", os.path.join(_TMP, "logs"))
# The stubbed runs below finish studies: keep their records out of the real
# study memory and skip cache.
os.environ["FARA_STUDY_MEMORY"] = os.path.join(_TMP, "study_memory.json")
os.environ["FARA_STUDY_CACHE"] = os.path.join(_TMP, "study_cache.json")

import sites                    # noqa: E402
import agent_core as ac         # noqa: E402
import behaviors.task_platform as tp  # noqa: E402
import form_fill_pw as ff       # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name:70s} {detail}", flush=True)


PRO_LIST = "https://app.prolific.com/studies"
PRO_TASK = "https://app.prolific.com/studies/" + "a" * 24
PRO_DONE = "https://app.prolific.com/submissions/complete?cc=C1NKBGUZ"
PRO_FORBIDDEN = "https://app.prolific.com/about-you"
PRO_WWW = "https://www.prolific.com/studies"
OTHER = "https://example.com/form"
URLS = (PRO_LIST, PRO_TASK, PRO_DONE, PRO_FORBIDDEN, PRO_WWW, OTHER, "about:blank", "")

# ============================================================== choosing one
print("\n--- choosing a profile")
check("the registry knows generic and prolific",
      {"generic", "prolific"} <= set(sites.names()), str(sites.names()))
try:
    sites.get("no-such-site")
    check("an unknown profile is refused", False)
except ValueError:
    check("an unknown profile is refused", True)
check("nothing set, nothing remembered -> generic",
      sites.resolve() == "generic" and sites.current.name == "generic")
check("a platform URL picks its profile", sites.resolve(start_url=PRO_TASK) == "prolific")
check("its marketing host too (same domain)", sites.owner_of(PRO_WWW) == "prolific")
check("an ordinary site picks none", sites.owner_of(OTHER) == "")
check("the page the browser is on counts when there is no start URL",
      sites.resolve(current_url=PRO_LIST) == "prolific")
check("the start URL outranks the browser's page",
      sites.resolve(start_url=OTHER, current_url=PRO_LIST) == "prolific")  # OTHER owns nothing
check("an explicit choice outranks every URL",
      sites.resolve("generic", PRO_TASK, PRO_LIST) == "generic")
check("'auto' is the same as no choice", sites.resolve("auto", PRO_TASK) == "prolific")
sites.use("prolific", remember=True)
check("a run remembers the profile it used",
      open(os.environ["FARA_LAST_SITE_FILE"]).read().strip() == "prolific")
check("...and an auto run with no URL to go on falls back to it",
      sites.resolve("", "", OTHER) == "prolific")
os.environ["FARA_SITE"] = "generic"
check("FARA_SITE outranks the URL", sites.resolve(start_url=PRO_TASK) == "generic")
sites.use("prolific", remember=True)
check("...and is not written over the remembered one",
      open(os.environ["FARA_LAST_SITE_FILE"]).read().strip() == "prolific")
del os.environ["FARA_SITE"]
os.remove(os.environ["FARA_LAST_SITE_FILE"])

# ======================================================= the generic profile
print("\n--- the generic profile switches the platform off")
sites.use("generic")
for u in URLS:
    tag = (u or "<empty>")[:40]
    check(f"not on a platform: {tag}", not ac._on_guarded_site(u))
    check(f"no launch surface: {tag}", not tp._on_launch_surface(u))
    check(f"nothing forbidden: {tag}", not ac.is_forbidden_url(u))
    check(f"no wrong host: {tag}", not ac._off_app_host(u))
    check(f"no invented path: {tag}", not ac._unknown_app_path(u))
    check(f"no completion code in the URL: {tag}", ac.completion_code(u) == "")
    check(f"no task list: {tag}", ac.list_state_from(u, "Waiting for studies") == "unknown")
check("no page-identity guess either", not tp._reached_in_launch_tab(PRO_DONE, 1, 1))
check("no task list to go back to", ac.studies_url() == "")
check("a loop returns to the page it started on", ac.loop_home_for(OTHER) == OTHER)
check("no standing rules", ac._standing_rules() == "")
check("no NOCODE refusal", tp.guarded_nocode_refusal("NOCODE", PRO_TASK, False, False, 0) == "")
check("no completion push-back on an ordinary page",
      tp.completion_pushback("I am done", OTHER, [], [], False) == "")
check("no 'no completion code' panel decision",
      tp.nocode_link_decision("NOCODE", PRO_TASK, True, False, [], [], 0, False) == "")
check("no launch flow", tp._humanized_launch_reserved_study() == {"status": "unsupported"})
check("no launch control to look for", tp._launch_control_visible() is False)
check("no dead ends", tp._detect_dead_end("Sorry, you are not eligible to take part in this study.") == "")
check("no reservation nudge", tp._reservation_nudge(True, "x") == "")
check("no task title script", tp._study_title() == "")
check("no completion-return control", tp._completion_return_box() is None
      and tp._completion_return_near_miss(10, 10) is None and tp._nocode_link_box() is None)
codes = tp.codes_on_page("Your Prolific code is ABCD1234. Completion code: ZX81AB")
check("only generic code wording is read", codes == ["ZX81AB"], str(codes))
check("a participant-ID field means nothing", ff._identity_kind(
    {"type": "text", "text": "Prolific ID", "id": ""}) is None)
check("an email field still does", ff._identity_kind(
    {"type": "text", "text": "Your e-mail", "id": ""}) == "email")
check("the identity hint names only what applies",
      "Prolific" not in ac._identity_hint_text({"participant_id": "X" * 24, "email": "a@b.c"}))
check("no platform word in any generic text", not any(
    "rolific" in (getattr(sites.current, m)(*a) or "") for m, a in (
        ("unattended_answer", ()), ("history_back_refusal", ()), ("refuse_unknown_path", ()),
        ("forbidden_notice", ()), ("wander_notice", (60,)), ("off_host_notice", ("u", "h")),
        ("resume_on_task_page", ()), ("resume_in_task", ()), ("launched_notice", ()),
        ("nocode_link_clicked", ("x",)), ("completion_return_clicked", ("x", "u")),
        ("nocode_codes_refusal_text", ("C",)), ("corrected_host", ("a", "b", "c")),
        ("corrected_unknown_path", ("a", "b", "c")), ("refuse_forbidden_visit",

()))))

# ===================================================== switching back and forth
print("\n--- switching profiles")
sites.use("prolific")
tp._learn_app_host("https://app.prolific.co/studies")
check("the Prolific profile applies its rules", tp._on_launch_surface(PRO_LIST)
      and ac.is_forbidden_url(PRO_FORBIDDEN) and ac.completion_code(PRO_DONE) == "C1NKBGUZ")
sites.use("generic")
check("switching away turns them off again", not tp._on_launch_surface(PRO_LIST))
sites.use("prolific")
check("a fresh profile does not inherit state (the learnt app host)",
      sites.current.app_host == "app.prolific.com")


# ===================================================== a whole run, stubbed
print("\n--- a run on an ordinary site, browser and model stubbed")
from PIL import Image  # noqa: E402


class _FakeBridge(types.SimpleNamespace):
    """Just enough of pw_bridge for run_agent on a page with nothing on it."""
    def __init__(self, url):
        super().__init__(url=url)

    def current_url(self):
        return self.url

    def __getattr__(self, name):            # goto, wait_for_content, front, evaluate...
        return lambda *a, **k: None

    def run(self, fn, *a, **k):
        return None

    def active_page_id(self):
        return 1


def _tool_call(args):
    return "<think>\n\n</think>\n\nok\n<tool_call>\n" + json.dumps(
        {"name": "computer_use", "arguments": args}) + "\n</tool_call>"


def run_stubbed(profile, url):
    sites.use(profile)
    prompts, live = [], []
    # A free-text terminate off a platform gets one "not done yet" push-back
    # under the Prolific profile, so the model is asked once more there.
    done_reply = _tool_call({"action": "terminate", "status": "success",
                             "answer": "The form is filled in."})
    replies = iter([_tool_call({"action": "left_click", "coordinate": [500, 400]}),
                    done_reply, done_reply])
    saved = {k: getattr(ac, k) for k in ("pw_bridge", "call_fara", "take_screenshot", "docker_exec",
                                         "_human_move", "_notify_telegram", "_get_cursor_pos",
                                         "_page_fingerprint", "_end_page_text", "_page_text",
                                         "_dom_nav_hint", "_close_extra_tabs")}
    saved_ff_bridge = ff.pw_bridge
    try:
        ac.pw_bridge = _FakeBridge(url)
        ff.pw_bridge = ac.pw_bridge          # identity_values reads the tabs' URLs
        ac.call_fara = lambda messages, step=None: (
            prompts.append(messages[-1]["content"][-1]["text"]), next(replies))[1]
        ac.take_screenshot = lambda run_dir, step: Image.new("RGB", (1440, 900), "white")
        ac.docker_exec = lambda *a, **k: ""
        ac._human_move = lambda x, y: None
        ac._notify_telegram = lambda m: live.append({"type": "telegram", "text": m})
        ac._get_cursor_pos = lambda: (500, 400)
        ac._page_fingerprint = lambda limit=8000: "a page with a form"
        ac._end_page_text = lambda limit=40000: ""
        ac._page_text = lambda limit=20000: ""
        ac._dom_nav_hint = lambda: ""
        ac._close_extra_tabs = lambda: None
        ac.live_hook = live.append
        events = list(ac.run_agent("Fill in the form.", "", 5,
                                   run_dir=type(ac.LOG_ROOT)(_TMP) / f"run_{profile}"))
    finally:
        for k, v in saved.items():
            setattr(ac, k, v)
        ff.pw_bridge = saved_ff_bridge
        ac.live_hook = None
    return events, prompts, live


ev, prompts, live = run_stubbed("generic", OTHER)
done = [e for e in ev if e.get("type") == "done"]
check("generic: the run ends on the model's own terminate",
      bool(done) and done[-1]["answer"] == "The form is filled in.", str(done[-1:])[:90])
check("generic: no task timer was started", not any(e.get("type") == "timer_start" for e in live))
check("generic: no 'task started' notification", not any("Studio iniziato" in e.get("text", "") for e in live))
check("generic: the first prompt is the task and nothing of a platform",
      prompts and prompts[0].startswith("Fill in the form.") and "STANDING RULES" not in prompts[0]
      and "rolific" not in prompts[0] and "resuming" not in prompts[0], repr(prompts[0][:80]))
check("generic: no timer line on later steps", len(prompts) >= 2 and "timer" not in prompts[1].lower())
check("generic: the terminate is taken at once, with no push-back", len(prompts) == 2, f"{len(prompts)} model calls")

ev, prompts, live = run_stubbed("prolific", OTHER)
check("prolific (control): the same run starts the task timer",
      any(e.get("type") == "timer_start" for e in live))
check("prolific (control): and sends Prolific's rules", prompts and "STANDING RULES" in prompts[0])
check("prolific (control): and pushes back once on a free-text terminate off Prolific",
      len(prompts) == 3 and "NOT DONE YET" in prompts[2], f"{len(prompts)} model calls")


# ---------------------------------------------------------------- behaviours
import behaviors                                  # noqa: E402
from behaviors.task_platform import TaskPlatform  # noqa: E402

check("generic runs the loop with no behaviour", sites.get("generic").behaviors() == ())
check("prolific runs it with the task-platform behaviour",
      tuple(sites.get("prolific").behaviors()) == (TaskPlatform,))
check("every hook of the base behaviour is a no-op",
      all(callable(getattr(behaviors.Behavior, h)) for h in
          ("start", "arrive", "settle", "after_screenshot", "first_prompt", "prompt_hints",
           "prompt_suffix", "before_action", "after_action", "diag", "terminate",
           "finish_record", "finished")))


class _Veto(behaviors.Behavior):
    def allows(self, run, feature):
        return feature != "batch_fill"


class _Wants(behaviors.Behavior):
    def wants(self, run, feature):
        return feature == "prescroll"


_r = behaviors.Run("t", None, None, [], [], {})
check("with no behaviour everything generic is allowed and nothing extra wanted",
      _r.allow("batch_fill") and _r.allow("reading_dwell") and not _r.want("prescroll"))
_r = behaviors.Run("t", None, None, [], [_Veto(), _Wants()], {})
check("one veto is enough to switch a feature off",
      not _r.allow("batch_fill") and _r.allow("reading_dwell"))
check("one behaviour asking is enough to switch a feature on",
      _r.want("prescroll") and not _r.want("auto_enter"))

# The loop itself must not know about task platforms: no timer, no guarded
# host, no completion code, no study.
_loop = inspect.getsource(ac.run_agent).split('"""', 2)[2]
_words = ("timer", "guarded", "completion_code", "study_timer", "here_on_launch",
          "nocode", "reserv", "wander", "skip_av", "telegram")
check("run_agent's body names nothing of a task platform",
      not [w for w in _words if w in _loop.replace("timer=run.timer", "")],
      str([w for w in _words if w in _loop.replace("timer=run.timer", "")]))
check("the task-platform helpers live with the behaviour, not in the loop",
      not [n for n in ("StudyTimer", "WanderGuard", "terminate_refusal", "guarded_nocode_refusal",
                       "_humanized_launch_reserved_study", "_detect_dead_end", "codes_on_page",
                       "completion_pushback", "_click_hits_submit")
           if hasattr(ac, n)] and hasattr(tp, "StudyTimer"))

print(f"\n== SUMMARY ==  {sum(RESULTS)}/{len(RESULTS)} passed")
sys.exit(0 if all(RESULTS) else 1)
