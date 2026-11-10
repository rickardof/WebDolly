#!/usr/bin/env python3
"""Offline tests for the harness guards around the Prolific launch flow.

Runs without a browser: Playwright is stubbed, the DOM helpers are mocked,
and the xdotool input path is captured instead of executed. Pair it with
test_fixtures_live.py, which checks the same code against real captured
Prolific pages in the isolated test container.

    ./venv_pw/bin/python scripts/tools/test_harness_guards.py
    python3 scripts/tools/test_harness_guards.py        # also fine

Several sections replay real run transcripts from var/logs/pw/. Those are
not in git and get pruned, so each is SKIPPED when its run is gone; every
other assertion still runs.

Why these live in the repo: this suite was twice written into /tmp and twice
lost to a reboot. Tests that evaporate are not tests.
"""
import json, os, random, re, sys, types
from urllib.parse import urlparse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOGS = os.path.join(ROOT, "var", "logs", "pw")
sys.path.insert(0, os.path.join(ROOT, "agent"))

# Stub Playwright so the real modules import in a bare interpreter.
_pw = types.ModuleType("playwright"); _pw_sync = types.ModuleType("playwright.sync_api")
_pw_sync.sync_playwright = lambda: None
sys.modules.setdefault("playwright", _pw)
sys.modules.setdefault("playwright.sync_api", _pw_sync)

import pw_bridge                      # noqa: E402
import agent_core as ac               # noqa: E402
import form_fill_pw as ff             # noqa: E402

RESULTS = []
def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name:66s} {detail}", flush=True)

def skip(name, why):
    print(f"SKIP  {name:66s} {why}", flush=True)

def section(title):
    print(f"\n--- {title} " + "-" * max(0, 60 - len(title)), flush=True)

def trail(run):
    """Per-step {step,url,action_type,args} from a real transcript, or None."""
    path = os.path.join(LOGS, run, "transcript.jsonl")
    if not os.path.exists(path):
        return None
    rows = [json.loads(l) for l in open(path) if l.strip()]
    dec, diag = {}, {}
    for r in rows:
        st = r.get("step")
        if r.get("type") == "diag": diag.setdefault(st, r)
        elif "action_type" in r: dec[st] = r
    out = []
    for st in sorted(s for s in set(list(dec) + list(diag)) if s is not None):
        d, g = dec.get(st, {}), diag.get(st, {})
        out.append({"step": st, "url": g.get("url") or "",
                    "action_type": d.get("action_type"), "args": d.get("args") or {}})
    return out

def rows_of(run):
    path = os.path.join(LOGS, run, "transcript.jsonl")
    if not os.path.exists(path):
        return None
    return [json.loads(l) for l in open(path) if l.strip()]


APP = "https://app.prolific.com"
TASK = APP + "/data-collection-tool/collections/01a0ed24-3f6a-77f1-9e95-8f6b8743b0f0"
EXTERNAL = ("https://eval.services/?x=1", "https://nyu.qualtrics.com/jfe/form/SV_x",
            "https://app.booktest.io/survey/a1")
VW, VH = 1439, 899
BOX = {"x": 1000, "y": 600, "width": 180, "height": 44}
OFF = {"x": 600, "y": 940, "width": 200, "height": 48}


# ===================================================================== 1
section("surface vocabulary (the distinction behind four bugs)")
# Four separate bugs came from asking "is this prolific.com?" when the real
# question was "is a study running?" or "is this the app?".
check("studies list is the launch surface", ac._on_launch_surface(APP + "/studies"))
check("a study detail page is the launch surface",
      ac._on_launch_surface(APP + "/studies/" + "a" * 24))
for p in ("/submissions", "/submissions/complete?cc=X", "/account", "/settings", "/"):
    check(f"chrome is the launch surface: {p}", ac._on_launch_surface(APP + p))
check("a Prolific-HOSTED task is NOT the launch surface", not ac._on_launch_surface(TASK))
check("...though it IS on the guarded domain (the trap)", ac._on_guarded_site(TASK))
for u in EXTERNAL:
    check(f"external host is not the launch surface: {urlparse(u).hostname}",
          not ac._on_launch_surface(u) and not ac._on_guarded_site(u))


# ===================================================================== 2
section("app host guard (run_20261106_142347: 22 of 32 steps on the marketing site)")
WWW = "https://www.prolific.com"
check("app host is fine", not ac._off_app_host(APP + "/studies"))
check("www IS off the app host", ac._off_app_host(WWW + "/studies?filter=available"))
check("marketing page is off the app host", ac._off_app_host(WWW + "/ai-task-builder"))
check("bare prolific.com is off the app host", ac._off_app_host("https://prolific.com/"))
for u in EXTERNAL + ("about:blank", "", None):
    check(f"never touches {str(u)[:30]!r}", not ac._off_app_host(u))
check("rewrite keeps path and query",
      ac._to_app_host(WWW + "/studies?filter=available") == APP + "/studies?filter=available")
check("rewrite is a no-op on the app host", ac._to_app_host(APP + "/studies") == APP + "/studies")

_saved = ac._APP_HOST
ac._learn_app_host("https://app.prolific.co/studies")
check("app host is learnt from start_url", ac._APP_HOST == "app.prolific.co")
check("...and then www on THAT domain is flagged",
      ac._off_app_host("https://www.prolific.co/x") and not ac._off_app_host("https://app.prolific.co/x"))
ac._learn_app_host("https://eval.services/survey")
check("a non-guarded start_url does not move it", ac._APP_HOST == "app.prolific.co")
ac._APP_HOST = _saved

t = trail("run_20261106_142347")
if t is None:
    skip("replay run_20261106_142347", "transcript pruned")
else:
    off = [s for s in t if ac._off_app_host(s["url"])]
    check("the real run was stranded off the app host", len(off) >= 15,
          f"{len(off)}/{len(t)} steps")
    mistyped = [s["args"].get("url") for s in t
                if s["action_type"] == "visit_url" and "www.prolific.com" in (s["args"].get("url") or "")]
    check("it shows the mistyped host", bool(mistyped), f"{mistyped}")
    check("all of them would now be corrected",
          all(urlparse(ac._to_app_host(u)).hostname == "app.prolific.com" for u in mistyped))


# ===================================================================== 3
section("batch-fill gate (run_20261106_154221: 139 vision clicks, auto-returned)")
allowed = lambda u: not ac._on_launch_surface(u)
for p in ("/studies", "/studies/" + "a" * 24, "/submissions", "/account", "/settings", "/"):
    check(f"blocked on chrome: {p}", not allowed(APP + p))
check("runs inside a Prolific-hosted task", allowed(TASK))
check("...which the old host gate blocked", ac._on_guarded_site(TASK))
for u in EXTERNAL:
    check(f"still runs on {urlparse(u).hostname}", allowed(u))

rows = rows_of("run_20261106_154221")
if rows is None:
    skip("replay run_20261106_154221", "transcript pruned")
else:
    diag = [r for r in rows if r.get("type") == "diag" and r.get("url")]
    on_task = [r for r in diag if "/data-collection-tool/" in r["url"]]
    check("the real run never ran one batch-fill check",
          len([r for r in rows if "batch_fill_check" in r]) == 0)
    check("yet spent the study on a Prolific-hosted task", len(on_task) >= 150,
          f"{len(on_task)}/{len(diag)}")
    check("every such step is now eligible",
          all(allowed(r["url"]) for r in on_task))
    check("it answered with 139 separate clicks",
          sum(1 for r in rows if r.get("action_type") == "left_click") == 139)


# ===================================================================== 4
section("wander guard")
_LIST = APP + "/studies"
check("fires nowhere but the launch surface",
      ac._on_launch_surface(_LIST) and not ac._on_launch_surface(TASK))
src = open(os.path.join(ROOT, "agent", "agent_core.py")).read()
check("the guard keys on the launch surface, not the host",
      "if not _on_launch_surface(url) or _LIST_PATH.match(" in src)
t = trail("run_20261106_084555")
if t is None:
    skip("replay run_20261106_084555", "transcript pruned")
else:
    task_steps = [s for s in t if "/data-collection-tool/" in s["url"]]
    check("that run really was on a Prolific-hosted task", bool(task_steps),
          f"{len(task_steps)} steps")
    check("none of those steps can arm the wander guard now",
          all(not ac._on_launch_surface(s["url"]) for s in task_steps))


# ===================================================================== 5
section("reserved-slot state and the humanised launcher")
launch_nudge = ac._reservation_nudge(True, "a" * 24)
cancel_nudge = ac._reservation_nudge(False, "a" * 24)
check("fresh nudge forbids cancelling", "do NOT cancel" in launch_nudge)
check("fresh nudge covers an unscrollable dialog",
      "SCROLL DOWN" in launch_nudge and "Tab" in launch_nudge and "Enter" in launch_nudge)
check("orphan nudge keeps finish-or-cancel",
      "RESERVATION LOOP" in cancel_nudge and "Cancel participation" in cancel_nudge)
check("the two branches differ", launch_nudge != cancel_nudge)
check("state is keyed on the DOM control, not the banner",
      "fresh_unlaunched = (_launch_control_visible()" in src
      and "not _saw_external_this_study and not orphan_visible" not in src)
check("the held-slot nudge branches on launch state",
      "if orphan_visible and fresh_unlaunched:" in src)
check("fresh threshold 4, orphan 6", "stuck_limit = 4 if fresh_unlaunched else 6" in src)
check("selectors still match what was captured",
      pw_bridge._SEL_RESERVE == '[data-testid="reserve"]'
      and pw_bridge._SEL_OPEN_DIALOG == '[data-testid="open-study-in-new-window-button"]'
      and pw_bridge._SEL_DIALOG == '[data-testid="start-study-dialog"]'
      and pw_bridge._SEL_START == '[data-testid="dialog-start-study"]')
check("pw_bridge stays read-only (no browser-side clicks)",
      "expect_popup" not in open(os.path.join(ROOT, "agent", "pw_bridge.py")).read())

# --- viewport guards
_moves = []
ac._human_move = lambda x, y: _moves.append(("move", x, y))
ac.docker_exec = lambda *a: _moves.append(("exec",) + tuple(a))
ac.cancel.sleep = lambda *a, **k: None
_CLICK = ("exec", "xdotool", "click", "1")
_ENTER = ("exec", "xdotool", "key", "--clearmodifiers", "Return")

class PW:
    _SEL_RESERVE = pw_bridge._SEL_RESERVE
    _SEL_OPEN_DIALOG = pw_bridge._SEL_OPEN_DIALOG
    _SEL_DIALOG = pw_bridge._SEL_DIALOG
    _SEL_START = pw_bridge._SEL_START
    def __init__(self, present=(), boxes=None, follow="https://survey.example/s",
                 checks=(), scrollable=True, focusable=True):
        self.present = set(present); self.boxes = {k: dict(v) for k, v in (boxes or {}).items()}
        self.follow = follow; self.checks = [dict(c) for c in checks]
        self.scrollable = scrollable; self.focusable = focusable; self.focused = []
    def front(self): return ""
    def viewport_size(self): return {"width": VW, "height": VH}
    def has_selector(self, s): return s in self.present
    def wait_selector(self, s, t=6000): return True
    def element_box(self, s, scroll=True, timeout_ms=4000):
        b = self.boxes.get(s); return dict(b) if b else None
    def unchecked_checkbox_boxes(self, d, scroll=True): return [dict(c) for c in self.checks]
    def page_ids(self): return [1, 2]
    def follow_new_tab(self, before, t=9000): return self.follow
    def focus_element(self, s):
        self.focused.append(s); return self.focusable
    def scrolled(self, ticks):
        if not self.scrollable: return
        for b in self.boxes.values(): b["y"] -= 120 * ticks
        for c in self.checks: c["y"] -= 120 * ticks

_ORIG_PW = ac.pw_bridge
def run_launch(pw, **kw):
    ac.pw_bridge = pw
    ac._wheel_scroll = lambda down, ticks: (_moves.append(("wheel", down, ticks)),
                                            pw.scrolled(ticks if down else -ticks))
    try:
        return ac._humanized_launch_reserved_study(**kw)
    finally:
        ac.pw_bridge = _ORIG_PW
        ac._wheel_scroll = lambda down, ticks: _moves.append(("wheel", down, ticks))

ac.pw_bridge = PW()
check("_box_on_screen: inside", ac._box_on_screen(BOX, VW, VH))
check("_box_on_screen: below the fold", not ac._box_on_screen(OFF, VW, VH))
check("_box_on_screen: unknown viewport does not block", ac._box_on_screen(OFF, 0, 0))
_moves.clear()
check("click_box refuses an off-screen box and clicks nothing",
      ac._humanized_click_box(dict(OFF)) is False and _moves == [])
_moves.clear()
check("click_box clicks inside an on-screen box",
      ac._humanized_click_box(dict(BOX)) is True and _CLICK in _moves)
ac.pw_bridge = _ORIG_PW

# progress-driven scrolling, at the rate measured on the real dialog (9px/tick)
ac.pw_bridge = PW()
_moves.clear()
creep = {"box": {"x": 600, "y": 1120, "width": 200, "height": 48}}
ac._wheel_scroll = lambda down, ticks: (_moves.append(("wheel", down, ticks)),
                                        creep["box"].update(y=creep["box"]["y"] - 9.0 * ticks))
got = ac._scroll_box_into_view(lambda: dict(creep["box"]))
check("a dialog creeping at 9px/tick is still scrolled all the way in",
      got is not None and ac._box_on_screen(got, VW, VH),
      f"rounds={len([m for m in _moves if m[0] == 'wheel'])}")
_moves.clear()
ac._wheel_scroll = lambda down, ticks: _moves.append(("wheel", down, ticks))
check("a dialog that moves nothing is given up on after two dead rounds",
      ac._scroll_box_into_view(lambda: dict(OFF)) is None
      and len([m for m in _moves if m[0] == "wheel"]) == 2)
_moves.clear()
endless = {"y": 5000.0}
ac._wheel_scroll = lambda down, ticks: (_moves.append(("wheel", down, ticks)),
                                        endless.update(y=endless["y"] - 1))
check("an endlessly creeping dialog stops at the tick budget",
      ac._scroll_box_into_view(lambda: {"x": 600, "y": endless["y"], "width": 200, "height": 48}) is None
      and sum(m[2] for m in _moves if m[0] == "wheel") <= ac._SCROLL_TICK_BUDGET)
ac._wheel_scroll = lambda down, ticks: _moves.append(("wheel", down, ticks))
ac.pw_bridge = _ORIG_PW

# launcher control flow
_moves.clear()
r = run_launch(PW(present={pw_bridge._SEL_OPEN_DIALOG},
                  boxes={pw_bridge._SEL_OPEN_DIALOG: BOX, pw_bridge._SEL_START: BOX}))
check("reserved study -> launched", r.get("status") == "launched", f"{r}")
check("via a real xdotool click, not CDP/DOM", _CLICK in _moves)

_moves.clear()
pw = PW(present={pw_bridge._SEL_OPEN_DIALOG, pw_bridge._SEL_DIALOG},
        boxes={pw_bridge._SEL_OPEN_DIALOG: BOX, pw_bridge._SEL_START: OFF}, scrollable=False)
r = run_launch(pw)
check("below-fold confirm + nothing scrolls -> keyboard fallback",
      r.get("status") == "launched" and pw.focused == [pw_bridge._SEL_START]
      and _ENTER in _moves, f"{r}")
check("it never aimed the cursor off-screen",
      not any(m[0] == "move" and m[2] > VH for m in _moves))

_moves.clear()
pw = PW(present={pw_bridge._SEL_OPEN_DIALOG, pw_bridge._SEL_DIALOG},
        boxes={pw_bridge._SEL_OPEN_DIALOG: BOX, pw_bridge._SEL_START: OFF}, scrollable=True)
r = run_launch(pw)
check("below-fold confirm the page can scroll to -> ordinary click",
      r.get("status") == "launched" and _ENTER not in _moves and pw.focused == [])

for kw, want in (({"include_take_part": True}, "no-reserve-control"),
                 ({"include_take_part": False}, "no-launch-control")):
    check(f"missing controls -> {want}",
          run_launch(PW(present=set(), boxes={}), **kw).get("status") == want)
check("start clicked but no tab -> clicked-no-tab",
      run_launch(PW(present={pw_bridge._SEL_OPEN_DIALOG},
                    boxes={pw_bridge._SEL_OPEN_DIALOG: BOX, pw_bridge._SEL_START: BOX},
                    follow=None)).get("status") == "clicked-no-tab")


# ===================================================================== 6
section("identity fields (a Prolific ID must never be invented)")
for typ in ("text", "textarea"):
    for label in ("Prolific ID", "Please enter your PROLIFIC_PID",
                  "participant id", "What is your Prolific id?"):
        check(f"{typ} {label[:28]!r} -> prolific_id",
              ff._identity_kind({"type": typ, "text": label, "id": ""}) == "prolific_id")
check("textarea email is an identity field",
      ff._identity_kind({"type": "textarea", "text": "Your e-mail", "id": ""}) == "email")
for f in ({"type": "text", "text": "How old are you?", "id": "age"},
          {"type": "radio", "text": "Prolific ID", "id": ""},
          {"type": "select", "text": "Prolific ID", "id": ""}):
    check(f"not an identity field: {f['type']}/{f['text'][:20]!r}",
          ff._identity_kind(f) is None)


# ===================================================================== 7
section("Prolific's own deadline (run_20261106_154221 overran it by ~8 min)")
for text, want in (("Study details\nTime remaining\n13 Mins 49 Secs\nProlific ID", 13 * 60 + 49),
                   ("Time remaining: 3 Mins 05 Secs", 185),
                   ("Time remaining 1 Hr 02 Mins", 3720),
                   ("Time remaining\n45 Secs", 45),
                   ("3 mins\n53 places\nSurvey", None),
                   ("Time remaining", None)):
    ac._page_text = (lambda t: (lambda n=6000: t))(text)
    got = ac._extract_time_remaining()
    check(f"parse {text.splitlines()[0][:32]!r}", got == want, f"got={got}")

class Clk:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t

clk = Clk(); tm = ac.StudyTimer(clock=clk); tm.start(180, deadline_seconds=15 * 60)
check("deadline known at start", tm.deadline_remaining() == 900)
clk.t = 450; check("pressure 0.5 halfway", abs(tm.deadline_pressure() - 0.5) < 1e-9)
clk.t = 1360; check("past the deadline -> pressure > 1 and remaining < 0",
                    tm.deadline_pressure() > 1 and tm.deadline_remaining() < 0)
u = ac.StudyTimer(clock=Clk()); u.start(180)
check("no countdown on the page -> nothing invented",
      u.deadline_remaining() is None and u.deadline_line() is None
      and "DEADLINE" not in (u.status_line() or ""))
c2 = Clk(); d = ac.StudyTimer(clock=c2); d.start(180, deadline_seconds=900)
c2.t = 200;        check("plenty of time -> plain mention", "Deadline in 12 min" in d.status_line())
c2.t = 900 - 420;  check("7 min left -> keep moving", "do not re-read" in d.status_line().lower())
c2.t = 900 - 120;  check("2 min left -> submit now", "Finish and submit NOW" in d.status_line())
c2.t = 1200;       check("past it -> said plainly", "DEADLINE PASSED" in d.status_line())
check("the minimum is still reported alongside", "Study timer:" in d.status_line())
check("dwell yields to a tight deadline",
      "_dwell_ok = _dl_pressure is None or _dl_pressure < 0.75" in src)
check("the countdown is scraped while still on Prolific",
      "_extract_time_remaining()" in src and "_last_guarded_deadline = _pcd" in src)
check("and handed to the timer", "deadline_seconds=_last_guarded_deadline or 0" in src)
check("and cleared between studies", src.count("_last_guarded_deadline = None") >= 2)


# ===================================================================== 8
section("invented URLs and Prolific error pages (run_20261108_154719)")
# The model composed /study/<uuid> from a study it had just been reading,
# Prolific served /404, and because _on_launch_surface answers
# "unknown path => a task is running", the harness started a study timer on
# an error page and switched the wander guard off.
for p_ in ("/404", "/403", "/500", "/error", "/not-found", "/maintenance"):
    check(f"error page is NOT a running task: {p_}", ac._on_launch_surface(APP + p_))
check("a real first-party task is still a running task",
      not ac._on_launch_surface(TASK))
check("...which is the exp303 behaviour that must not regress",
      not ac._on_launch_surface(APP + "/data-collection-tool/collections/abc"))
check("an uncatalogued path still counts as a task (deliberate default)",
      not ac._on_launch_surface(APP + "/some-new-first-party-tool/x"))

INVENTED = APP + "/study/85023784-7114-4869-b0a1-300084006977"
check("the invented URL is refused before it loads", ac._unknown_app_path(INVENTED))
for good in ("/studies", "/studies/" + "a" * 24, "/", "/submissions",
             "/submissions/complete?cc=X", "/account", "/settings",
             "/data-collection-tool/collections/abc"):
    check(f"typed navigation still allowed: {good}",
          not ac._unknown_app_path(APP + good))
for u in EXTERNAL:
    check(f"never blocks an external survey: {urlparse(u).hostname}",
          not ac._unknown_app_path(u))
check("the marketing site is left to its own guard",
      not ac._unknown_app_path("https://www.prolific.com/ai-task-builder"))
check("visit_url refuses it with navigation advice",
      "visit_url REFUSED: there is no such page on Prolific." in src
      and "_unknown_app_path(target)" in src)

t = trail("run_20261108_154719")
if t is None:
    skip("replay run_20261108_154719", "transcript pruned")
else:
    typed = [s_["args"].get("url") for s_ in t
             if s_["action_type"] == "visit_url" and s_["args"].get("url")]
    # The address it typed was www.prolific.com/study/<uuid>: wrong host AND
    # no such path. The host guard alone rewrote it to the app and produced a
    # 404, so the check has to hold on the REWRITTEN target.
    bad = [u for u in typed
           if ac._unknown_app_path(u) or ac._unknown_app_path(ac._to_app_host(u))]
    check("the real run typed an address that is now caught", bool(bad), f"{bad}")
    check("correcting the host no longer launders a bogus path",
          "if _unknown_app_path(fixed):" in src
          and "wrong host AND no such path" in src)
    err = [s_ for s_ in t if "/404" in s_["url"]]
    if err:
        check("and the 404 it landed on no longer reads as a running task",
              all(ac._on_launch_surface(s_["url"]) for s_ in err),
              f"{len(err)} steps")


# ===================================================================== 9
section("harness notices survive the run")
# The notices ARE the harness explaining itself, and they used to exist only
# in the live SSE stream: the 404-as-a-study bug was caught only because a
# monitor happened to be attached that minute.
check("_live has a transcript sink", "_live_sink" in src and "def _write_notice" in src)
check("it persists notices, timer starts and errors",
      'msg.get("type") in ("notice", "timer_start", "error")' in src)
check("the sink is installed when the transcript opens",
      src.index("_live_sink = _write_notice") > src.index('transcript = (run_dir / "transcript.jsonl").open("w")')
      and src.index("_live_sink = _write_notice") < src.index("for step in range(1, max_rounds + 1):"))
check("and torn down with it", "_live_sink = None\n        transcript.close()" in src)
check("notices carry the step they happened on", '_live_sink_step["n"] = step' in src)

_captured = []
_prev_sink, _prev_hook = ac._live_sink, ac.live_hook
ac._live_sink = _captured.append
ac.live_hook = None
ac._live({"type": "notice", "text": "[guard] off the app host"})
ac._live({"type": "timer_start", "min_seconds": 180})
ac._live({"type": "phase", "phase": "think"})
ac._live({"type": "error", "message": "boom"})
ac._live_sink, ac.live_hook = _prev_sink, _prev_hook
check("a notice reaches the sink", any(m.get("text", "").startswith("[guard]") for m in _captured))
check("a timer start reaches it", any(m.get("type") == "timer_start" for m in _captured))
check("an error reaches it", any(m.get("type") == "error" for m in _captured))
check("per-step phase chatter does NOT (it would swamp the file)",
      not any(m.get("type") == "phase" for m in _captured), f"{len(_captured)} records")

def _boom(_): raise RuntimeError("sink died")
_prev_sink, _prev_hook = ac._live_sink, ac.live_hook
ac._live_sink, ac.live_hook = _boom, None
try:
    ac._live({"type": "notice", "text": "x"})
    ok = True
except Exception:
    ok = False
ac._live_sink, ac.live_hook = _prev_sink, _prev_hook
check("a failing sink never breaks the run", ok)


# ===================================================================== 10
section("the remaining surface gates")
check("skip_av gates on the launch surface",
      "elif skip_av and not here_on_launch and study_timer.is_active():" in src
      and "elif skip_av and not here_guarded" not in src)
check("reading dwell gates on the launch surface",
      "not on_list and not here_on_launch and _dwell_ok" in src
      and "not on_list and not here_guarded" not in src)
check("batch-fill gates on the launch surface",
      "if batch_fill and not here_on_launch and not _user_inject_pending:" in src)
check("Prolific-side confirmations still key on the host (correctly)",
      "if here_guarded and not _saw_cancel_confirm:" in src)



# ===================================================================== 11
section("account chrome is not a running study")
# run_20261108_181732 step 42: the model clicked "About you" in Prolific's
# own top nav, _on_launch_surface answered "unknown path => a task", and the
# harness started a study timer on /about-you.
_NAV = ["/", "/balance-hub", "/studies", "/submissions", "/about-you",
        "/messages/unread", "/desktop-app", "/referrals",
        "/account/general", "/account/preferences"]
_missed = [n for n in _NAV
           if not ac._on_launch_surface("https://app.prolific.com" + n)]
check("every in-app nav link reads as launch surface", not _missed, _missed)
check("/about-you specifically (the run's regression)",
      ac._on_launch_surface("https://app.prolific.com/about-you"))
check("a Prolific-hosted task still reads as a running task",
      not ac._on_launch_surface(
          "https://app.prolific.com/data-collection-tool/abc/task"))
check("an external survey host still reads as a running task",
      not ac._on_launch_surface("https://app.booktest.io/survey/x"))

# The page-identity guard: an UNCATALOGUED app path reached without opening
# a new window is chrome; the same path in a window of its own is a task.
_SAME, _OTHER = 111, 222
check("unknown app path in the launch tab is chrome",
      ac._reached_in_launch_tab(
          "https://app.prolific.com/some-new-page", _SAME, _SAME))
check("the same path in a NEW window stays a task",
      not ac._reached_in_launch_tab(
          "https://app.prolific.com/some-new-page", _OTHER, _SAME))
check("the known hosted-task tool is never reclassified",
      not ac._reached_in_launch_tab(
          "https://app.prolific.com/data-collection-tool/x", _SAME, _SAME))
check("an external host is never reclassified",
      not ac._reached_in_launch_tab(
          "https://app.booktest.io/survey/x", _SAME, _SAME))
check("inert until a launch surface has been seen",
      not ac._reached_in_launch_tab(
          "https://app.prolific.com/some-new-page", _SAME, None))
check("the guard is wired into the step loop",
      "if not here_on_launch and _reached_in_launch_tab(" in src
      and "_launch_page_id = _cur_page_id" in src)
check("pw_bridge exposes the active page identity",
      "def active_page_id():" in
      open(os.path.join(ROOT, "agent", "pw_bridge.py"),
           encoding="utf-8").read())


# ===================================================================== 12
section("the pre-screenshot nudge does not fight the model")
_js = ac._AUTOSCROLL_TO_CONTROLS_JS
check("an answered radio group is excluded", "const answered = e =>" in _js
      and ':checked\'' in _js.replace('"', "'"))
check("aria radiogroups too", 'role="radiogroup"' in _js)
check("the nudge is skipped right after the model scrolls",
      "if _model_moved_viewport:" in src
      and 'prescroll_result = "skipped-after-scroll"' in src)
check("scroll and hscroll set the flag",
      'action_type in ("scroll", "hscroll")' in src)
check("so do the paging keys",
      '"home", "end", "page_up", "page_down"' in src)



# ===================================================================== 13
section("a validated study must not end unsubmitted")
# run_20261109_102733: 45 trials, "Your submission has been validated",
# then terminate NOCODE with the return button on screen. $3,00.
check("NOCODE on the survey host is no longer exempt",
      "NOT DONE YET" in ac.completion_pushback(
          "NOCODE", "https://eval.services/x", [], [], False))
check("but it is still accepted back on Prolific",
      ac.completion_pushback(
          "NOCODE", "https://app.prolific.com/studies/a", [], [], False) == "")
check("and when a code was seen anywhere",
      ac.completion_pushback(
          "NOCODE", "https://eval.services/x", ["ABC123"], [], False) == "")
check("and the nudge stays one-shot, so a dead end can still end",
      ac.completion_pushback(
          "NOCODE", "https://eval.services/x", [], [], True) == "")
check("the finder keys on Prolific's completion endpoint",
      "submissions" in ac._COMPLETION_RETURN_JS
      and "complete" in ac._COMPLETION_RETURN_JS)
check("and on a label, for buttons that navigate in JS",
      "collect" in ac._COMPLETION_RETURN_JS
      and "prolific" in ac._COMPLETION_RETURN_JS.lower())
check("the harness clicks it itself before nudging in prose",
      "_rbox = _scroll_box_into_view(_completion_return_box)" in src
      and "_humanized_click_box(_rbox)" in src
      and "_completion_return_tried" in src)
check("one attempt per study, reset on the studies list",
      "_completion_return_tried: bool = False" in src
      and "_completion_return_tried = False\n" in src
      and "_completion_return_tried = True" in src)




# ===================================================================== 14
section("a near-miss on the return control is re-aimed, not wasted")
# run_20261109_120846 step 50: button y 566..601, clicked at y 609. Eight
# pixels. The model then spent six steps deciding the button was broken.
_njs = ac._COMPLETION_NEAR_MISS_JS
check("the near-miss test reuses the same finder",
      "submissions" in _njs and "collect" in _njs)
check("a point already inside the control is not 'corrected'",
      "return null;   // already on it" in _njs)
check("the margin is distance to the box, not to its centre",
      "Math.hypot(dx, dy) > margin" in _njs)
check("a click that landed on another control is never hijacked",
      "elementFromPoint" in _njs
      and "'BUTTON'" in _njs and "'LABEL'" in _njs
      and "tabIndex >= 0" in _njs)
check("the margin leaves room for a worse estimate than 8px",
      20 <= ac._RETURN_NEAR_MISS_PX <= 60, ac._RETURN_NEAR_MISS_PX)
check("the snap only rewrites the coordinate, so dispatch stays humanised",
      'args["coordinate"] = [_nx, _ny]' in src)
check("it runs before the minimum-time hold, which can still refuse it",
      src.index("_sbox = _completion_return_near_miss(")
      < src.index('submit_held = ""'))
check("it never fires on Prolific's own pages",
      'if action_type == "left_click" and not here_guarded:\n'
      '                _coord = args.get("coordinate")' in src)
check("and the correction is written into the step result",
      "dispatch_action(action_type, args) + snap_note" in src)


# --------------------------------------------------------------------------
section("the last inch on Prolific (run_20261110_184734: a finished survey, "
        "never submitted)")
# The Google Form showed no completion code. The model filled it, submitted
# it, came back to the Prolific tab, clicked "I'm finished" -- and then called
# terminate NOCODE with the "Confirm submission" panel open and the link
# "I didn't receive a completion code" one click below. Slot still reserved,
# study never submitted. guarded_nocode_refusal exempts it (the survey WAS
# opened) and completion_pushback is spent on the survey host.
PRO = APP + "/studies/6af3550a148b4a6e6c385cdb"
d = ac.nocode_link_decision

check("the panel's own link is the signal: press it",
      d("NOCODE", PRO, True, False, [], [], 0, False) == "click")
check("the model's own 'CODE: NOCODE' spelling counts too",
      d("CODE: NOCODE", PRO, True, False, [], [], 0, False) == "click")
check("no panel on screen -> the submission is already made, let it end",
      d("NOCODE", PRO, False, False, [], [], 0, False) == "")
check("a cancel/submit confirmation seen -> nothing left to press",
      d("NOCODE", PRO, True, True, [], [], 0, False) == "")
check("a real code answer is never touched",
      d("CODE: C1NKBGUZ", PRO, True, False, [], [], 0, False) == "")
check("off Prolific it is completion_pushback's job, not this guard's",
      d("NOCODE", "https://eval.services/x", True, False, [], [], 0, False) == "")
check("still there after we pressed it -> say so in words, do not re-click",
      d("NOCODE", PRO, True, False, [], [], 1, True) == "retry")
check("safety valve: the third attempt passes whatever the page shows",
      d("NOCODE", PRO, True, False, [], [], 2, False) == "")
check("a code WAS seen on the study pages -> never submit without it",
      d("NOCODE", PRO, True, False, ["C1NKBGUZ"], [], 0, False) == "codes")
check("...including one only on the page in front of us",
      d("NOCODE", PRO, True, False, [], ["C1NKBGUZ"], 0, False) == "codes")
check("and the refusal names that code instead of describing it",
      "C1NKBGUZ" in ac.nocode_codes_refusal(["C1NKBGUZ"]))

_ljs = ac._NOCODE_LINK_JS
check("the probe reads the control's own label, in English and Italian",
      "receive" in _ljs and "non\\s+ho\\s+ricevuto" in _ljs)
check("it only ever looks at controls, not at prose that mentions a code",
      "'a, button, [role=\"button\"], input[type=\"submit\"]'" in _ljs
      and "t.length > 120" in _ljs)
check("a hidden or disabled control does not count as present",
      "visibility === 'hidden'" in _ljs and "aria-disabled" in _ljs)
check("the probe is read-only: finding the link never clicks it",
      ".click()" not in _ljs and "dispatchEvent" not in _ljs)

check("the refusal names the link and the cancel branch, not just the rule",
      "I didn't receive a completion code" in ac._NOCODE_LINK_REFUSAL
      and "Cancel participation" in ac._NOCODE_LINK_REFUSAL)
check("the harness presses it with the real cursor, scrolled into view",
      "_lbox = _scroll_box_into_view(_nocode_link_box)" in src
      and "_humanized_click_box(_lbox)" in src)
check("it runs after the never-opened-the-study guard, not instead of it",
      src.index("nocode_block = guarded_nocode_refusal(")
      < src.index("_nc = nocode_link_decision("))
check("the dialog it opens is left to the model to confirm",
      "confirm the submission in the" in src)
check("the per-study state is cleared on the studies list",
      "_nocode_link_attempts = 0" in src and "_nocode_link_tried = False" in src)
check("the compact rules (the live ones) now carry the no-code branch",
      "I didn't receive a completion code" in ac._STANDING_RULES_COMPACT
      and "I didn't receive a completion code" in ac._STANDING_RULES_FULL)


print()
passed, total = sum(RESULTS), len(RESULTS)
print(f"== SUMMARY ==  {passed}/{total} passed", "ok" if passed == total else "FAILED")
sys.exit(0 if passed == total else 1)
