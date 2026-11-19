#!/usr/bin/env python3
"""Record what the agent loop does, end to end, in scripted scenarios -- and
check a change to the loop leaves every bit of it the same.

Each scenario drives agent_core.run_agent through a whole run with the outside
world replaced at its real boundaries, never inside the agent's own code:

  browser   the `pw_bridge` module, swapped for a scripted fake page model
  docker    subprocess.run ("docker exec ... xdotool/scrot")
  model     cancel.post_chat, answering from the scenario's script
  clock     time.monotonic / time.time / time.sleep / cancel.sleep (virtual)
  Telegram  urllib.request.urlopen (messages are captured, nothing is sent)

and random is seeded. Everything the loop produces is recorded: the prompt of
every model call, every yielded and live event, every Telegram message, every
browser navigation and every xdotool command, every transcript line.

Because the fakes sit at the boundaries, internal code can be moved around
freely and the recording must not change:

    python tests/loop_scenarios.py --record before.json      # on the old code
    python tests/loop_scenarios.py --compare before.json     # on the new code

No browser, no model, no docker. Each scenario runs in its own process.
"""
import json
import os
import random
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(HERE, "..", "agent")

PRO = "https://app.prolific.com"
LIST = PRO + "/studies"
DETAIL = PRO + "/studies/6af3550a148b4a6e6c385cdb"
SURVEY = "https://survey.example.com/s/abc?PROLIFIC_PID=5f2a9b3c4d5e6f708192a3b4"
DONE_URL = PRO + "/submissions/complete?cc=C1NKBGUZ"
BOX = {"x": 600, "y": 500, "width": 200, "height": 44}


def click(x=500, y=400):
    return {"action": "left_click", "coordinate": [x, y]}


def term(answer):
    return {"action": "terminate", "status": "success", "answer": answer}


def key(*keys):
    return {"action": "key", "keys": list(keys)}


def visit(url):
    return {"action": "visit_url", "url": url}


LIST_PAGE = {"text": "Studies\nAI study about shopping\n£2.00 · £12.00/hr · 10 minutes\nTake part"}
DETAIL_PAGE = {"text": "AI study about shopping\nTake part in this study\nEstimated 1 minutes\n"
                       "Time remaining 13 Mins 49 Secs", "title": "AI study about shopping"}
SURVEY_PAGE = {"text": "Question 1 of 3\nHow often do you shop online?\nNext"}

# name -> scenario. "pages" maps a URL to what the page shows; "steps" is the
# model's script: (reply, URL the browser is on after that action or None).
SCENARIOS = {
    "study_with_timer": dict(
        profile="prolific", start=LIST,
        pages={LIST: LIST_PAGE, DETAIL: DETAIL_PAGE, SURVEY: SURVEY_PAGE},
        steps=[(click(), DETAIL), (click(), SURVEY), (term("CODE: ABC123"), None),
               ({"action": "wait", "time": 30}, None), (click(700, 800), None),
               (term("CODE: ABC123"), None)]),
    "code_in_the_url": dict(
        profile="prolific", start=LIST,
        pages={LIST: LIST_PAGE, DETAIL: DETAIL_PAGE, SURVEY: SURVEY_PAGE},
        steps=[(click(), DETAIL), (click(), SURVEY), (click(700, 800), DONE_URL),
               (term("unused"), None)]),
    "forbidden_and_wrong_host": dict(
        profile="prolific", start=LIST,
        pages={LIST: LIST_PAGE},
        steps=[(visit(PRO + "/about-you"), None),
               (visit("https://www.prolific.com/studies"), None),
               (visit("https://www.prolific.com/study/0b9d7f2e-1111"), None),
               (click(), "https://www.prolific.com/ai-task-builder"),
               (click(), PRO + "/about-you"),
               (term("NONE"), None)]),
    "nocode_without_the_survey": dict(
        profile="prolific", start=LIST,
        pages={LIST: LIST_PAGE, DETAIL: DETAIL_PAGE},
        steps=[(click(), DETAIL), (term("NOCODE"), None), (term("CODE: NOCODE"), None),
               (term("NOCODE"), None)]),
    "dead_end_study": dict(
        profile="prolific", start=LIST,
        pages={LIST: LIST_PAGE,
               DETAIL: {"text": "Sorry, you are not eligible to take part in this study.",
                        "title": "Coffee habits"}},
        steps=[(click(), DETAIL), (term("NONE"), None)]),
    "reserved_slot_is_launched": dict(
        profile="prolific", start=DETAIL,
        pages={DETAIL: dict(DETAIL_PAGE, text=DETAIL_PAGE["text"]
                            + "\nPlease either finish or return this submission",
                            launch={"present": ["open"], "boxes": {"open": BOX, "start": BOX},
                                    "follow": SURVEY}),
               SURVEY: SURVEY_PAGE},
        steps=[(key("ctrl", "tab"), None), (key("alt", "Left"), None), (click(), None),
               (term("CODE: ZZ1234"), None)]),
    "held_slot_without_a_way_in": dict(
        profile="prolific", start=DETAIL,
        pages={DETAIL: dict(DETAIL_PAGE, text="I'm finished\nCancel participation\n"
                            "Please either finish or return this submission")},
        steps=[({"action": "scroll", "pixels": -200}, None)] * 7 + [(term("NOCODE"), None)] * 3),
    "nocode_but_a_code_was_shown": dict(
        profile="prolific", start=LIST,
        pages={LIST: LIST_PAGE, DETAIL: dict(DETAIL_PAGE, nocode_box=BOX),
               SURVEY: dict(SURVEY_PAGE, text="Thanks! Your completion code is: KX42MP9")},
        steps=[(click(), DETAIL), (click(), SURVEY), (key("ctrl", "shift", "Tab"), DETAIL),
               ({"action": "wait", "time": 60}, None), (term("NOCODE"), None),
               (term("CODE: KX42MP9"), None)]),
    "survey_end_without_code": dict(
        profile="prolific", start=SURVEY,
        pages={SURVEY: dict(text="Thank you! Your answers were recorded.",
                            return_box=BOX, click_label="")},
        steps=[(term("I completed the study"), None), (term("I completed the study"), None),
               (term("I completed the study"), None)]),
    "nocode_with_the_panel_open": dict(
        profile="prolific", start=LIST,
        pages={LIST: LIST_PAGE, DETAIL: dict(DETAIL_PAGE, nocode_box=BOX), SURVEY: SURVEY_PAGE},
        steps=[(click(), DETAIL), (click(), SURVEY), (key("ctrl", "shift", "Tab"), DETAIL),
               (term("NOCODE"), None), (term("NOCODE"), None), (term("NOCODE"), None)]),
    "submit_held_by_the_timer": dict(
        profile="prolific", start=LIST,
        pages={LIST: LIST_PAGE,
               DETAIL: dict(DETAIL_PAGE, text="Take part in this study\nEstimated 10 minutes"),
               SURVEY: dict(SURVEY_PAGE, click_label="Submit")},
        steps=[(click(), DETAIL), (click(), SURVEY), (click(700, 800), None),
               (click(700, 800), None), (term("CODE: QQ7777"), None)]),
    "wander_guard": dict(
        profile="prolific", start=LIST, wander=40,
        pages={LIST: LIST_PAGE, DETAIL: DETAIL_PAGE},
        steps=[(click(), DETAIL)] + [({"action": "scroll", "pixels": -300}, None)] * 6
              + [(term("NONE"), None)]),
    "audio_study_skipped": dict(
        profile="prolific", start=LIST, skip_av=True,
        pages={LIST: LIST_PAGE,
               DETAIL: {"text": "You will need headphones for this task.", "title": "Listening"}},
        steps=[(click(), DETAIL), (term("NONE"), None)]),
    "resume_on_a_survey": dict(
        profile="prolific", start=SURVEY, pages={SURVEY: SURVEY_PAGE},
        steps=[(click(), None), (term("CODE: RS4321"), None)]),
    "generic_site": dict(
        profile="generic", start="https://example.com/form",
        pages={"https://example.com/form": {"text": "Contact form\nName\nEmail\nSend",
                                            "return_box": BOX, "nocode_box": BOX}},
        steps=[(click(), None), (key("Return"), None), (term("The form was sent."), None)]),
}


# ============================================================ one scenario
def run_one(name):
    sc = SCENARIOS[name]
    tmp = tempfile.mkdtemp()
    for k, v in {"FARA_SITE": sc["profile"], "FARA_LOG_ROOT": os.path.join(tmp, "logs"),
                 "FARA_DATA_DIR": os.path.join(tmp, "data"), "FARA_CAPTURE_DIR": os.path.join(tmp, "cap"),
                 "FARA_STUDY_CACHE": os.path.join(tmp, "cache.json"),
                 "FARA_STUDY_MEMORY": os.path.join(tmp, "memory.json"),
                 "FARA_LAST_SITE_FILE": os.path.join(tmp, "last_site"),
                 "FARA_CDP_URL": "http://127.0.0.1:9", "FARA_BRIDGE_WATCHDOG": "0"}.items():
        os.environ[k] = v
    os.makedirs(os.environ["FARA_DATA_DIR"])
    random.seed(1234)
    rec = {"prompts": [], "events": [], "live": [], "telegram": [], "browser": [], "docker": []}

    # --- the clock -----------------------------------------------------------
    import time
    clock = {"t": 1_000_000.0}
    time.monotonic = lambda: clock["t"]
    time.time = lambda: clock["t"]

    def _sleep(s):
        clock["t"] += float(s or 0)
    time.sleep = _sleep

    # --- docker / xdotool --------------------------------------------------
    real_run = subprocess.run

    def fake_run(cmd, *a, **k):
        if isinstance(cmd, list) and cmd[:2] == ["docker", "exec"]:
            rest = list(cmd[2:])
            while rest and rest[0] == "-e":          # -e NAME=VALUE pairs
                rest = rest[2:]
            args = rest[1:]                          # drop the container name
            out = ""
            if args[:1] == ["scrot"]:
                from PIL import Image
                Image.new("RGB", (1440, 900), "white").save(
                    os.path.join(os.environ["FARA_DATA_DIR"], "_agent_live.png"))
            else:
                rec["docker"].append(" ".join(map(str, args)))
                if "getmouselocation" in args:
                    out = "X=500\nY=400\nSCREEN=0\nWINDOW=123\n"
                elif "search" in args:
                    out = "123\n"
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return real_run(cmd, *a, **k)
    subprocess.run = fake_run

    # --- Telegram ------------------------------------------------------------
    import urllib.request
    tg = types.ModuleType("telegram_config")
    tg.TELEGRAM_TOKEN, tg.TELEGRAM_CHAT_ID = "TOKEN", "1"
    sys.modules["telegram_config"] = tg

    def fake_urlopen(req, *a, **k):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "api.telegram.org" in url:
            rec["telegram"].append(json.loads(req.data.decode())["text"])
            return types.SimpleNamespace(read=lambda: b"{}")
        raise OSError("no network in scenarios: " + url)
    urllib.request.urlopen = fake_urlopen

    # --- the browser -------------------------------------------------------------
    state = {"url": sc["start"], "pages": sc["pages"], "tabs": 1}

    def page():
        return state["pages"].get(state["url"], {})

    def sel_key(sel):
        import sites
        s = sites.current
        return {s.launch_reserve_sel: "reserve", s.launch_open_sel: "open",
                s.launch_dialog_sel: "dialog", s.launch_start_sel: "start"}.get(sel, sel)

    def page_eval(js, arg=None):
        p = page()
        if not isinstance(js, str):
            return None
        if js.strip() == "document.body.innerText":
            return p.get("text", "")
        if "document.documentElement.outerHTML" in js:
            return "<html></html>"
        if "imgs.push" in js:
            return p.get("text", "")
        if "'completion code: ' + v" in js:
            return p.get("text", "")
        if "box.miss" in js:
            return p.get("near_miss")
        if "collect\\s+your\\s+reward" in js:
            return p.get("return_box")
        if "receive(d)?" in js:
            return p.get("nocode_box")
        if "study-name" in js:
            return p.get("title", "")
        if "soundcloud" in js:
            return p.get("av", "")
        if "elementFromPoint" in js and "minute" in js:
            return p.get("text", "")
        if "classList.contains('disabled')" in js:
            return bool(p.get("disabled"))
        if "role=checkbox" in js:
            return None
        if "aria-label" in js and "BUTTON" in js:
            return p.get("click_label", "")
        if "nav.some" in js:
            return None
        if "likert-btn" in js:
            return "nothing"
        if "innerText.length" in js:
            return len(p.get("text", ""))
        if "innerWidth" in js:
            return {"width": 1440, "height": 900}
        return None

    class Frame:
        @property
        def url(self):
            return state["url"]

        def evaluate(self, js, arg=None):
            return page_eval(js, arg)

    class Page(Frame):
        frames = property(lambda self: [Frame()])
        context = property(lambda self: types.SimpleNamespace(pages=[self]))

        def wait_for_timeout(self, ms):
            clock["t"] += ms / 1000.0

        def bring_to_front(self):
            pass

        def close(self):
            pass

    the_page = Page()

    class B:
        _active = the_page
        _known = [the_page]

        def active_page(self):
            return the_page

        def pages(self):
            return [the_page] * state["tabs"]

    pb = types.ModuleType("pw_bridge")
    pb.CDP_URL = os.environ["FARA_CDP_URL"]

    def log(*what):
        rec["browser"].append(" ".join(map(str, what)))

    def goto(url):
        log("goto", url)
        state["url"] = url

    def reset_to(url):
        log("reset_to", url)
        state["url"], state["tabs"] = url, 1

    def follow_new_tab(before, timeout_ms=9000):
        to = page().get("launch", {}).get("follow")
        log("follow_new_tab", to)
        if to:
            state["url"] = to
        return to
    pb.run = lambda fn, *a, **k: fn(B())
    pb.evaluate = lambda js: page_eval(js)
    pb.current_url = lambda: state["url"]
    pb.goto = goto
    pb.reset_to = reset_to
    pb.reload = lambda: log("reload")
    pb.go_back = lambda: (log("go_back"), False)[1]
    pb.switch_tab = lambda d: (log("switch_tab", d), state["url"])[1]
    pb.wait_for_content = lambda *a, **k: 0.0
    pb.front = lambda: state["url"]
    pb.active_page_id = lambda: 1
    pb.page_ids = lambda: [1]
    pb.page_count = lambda: state["tabs"]
    pb.viewport_size = lambda: {"width": 1440, "height": 900}
    pb.has_selector = lambda sel: sel_key(sel) in page().get("launch", {}).get("present", [])
    pb.wait_selector = lambda sel, t=6000: True
    pb.element_box = lambda sel, scroll=True, timeout_ms=4000: (
        dict(page().get("launch", {}).get("boxes", {}).get(sel_key(sel)) or {}) or None)
    pb.unchecked_checkbox_boxes = lambda d, scroll=True: []
    pb.focus_element = lambda sel: (log("focus", sel_key(sel)), True)[1]
    pb.follow_new_tab = follow_new_tab
    pb.launch_control_present = lambda sels=(): any(
        sel_key(s) in page().get("launch", {}).get("present", []) for s in sels)
    pb.drop_connection = lambda: None
    pb.recycle_tab = lambda url: False
    sys.modules["pw_bridge"] = pb

    # --- the model -----------------------------------------------------------------
    sys.path.insert(0, AGENT)
    import cancel
    cancel.sleep = _sleep
    steps = iter(sc["steps"])

    def post_chat(url, payload, timeout=120, on_delta=None, extra_headers=None):
        msg = payload["messages"][-1]
        text = msg["content"][-1]["text"] if isinstance(msg["content"], list) else msg["content"]
        rec["prompts"].append(text)
        clock["t"] += 10.0                       # the model takes ten seconds
        try:
            reply, after = next(steps)
        except StopIteration:
            reply, after = term("SCRIPT ENDED"), None
        if after:
            state["url"] = after
        return {"choices": [{"message": {"content": "<think>\n\n</think>\n\nok\n<tool_call>\n"
                                         + json.dumps({"name": "computer_use", "arguments": reply})
                                         + "\n</tool_call>"}}], "timings": {}}
    cancel.post_chat = post_chat

    # --- run -----------------------------------------------------------------------
    import agent_core as ac
    ac.live_hook = lambda m: rec["live"].append(
        {k: v for k, v in m.items() if k not in ("ts", "elapsed", "remaining", "text")
         or m.get("type") == "notice"})
    run_dir = ac.LOG_ROOT / "run_scenario"
    gen = ac.run_agent("Take part in the study. Persona: 30, software engineer.",
                       "", sc.get("max_rounds", 12), run_dir=run_dir,
                       wander_seconds=sc.get("wander", 0), skip_av=sc.get("skip_av", False))
    for ev in gen:
        rec["events"].append({k: v for k, v in ev.items() if k not in ("messages", "screenshot")})
    rec["transcript"] = [json.loads(l) for l in
                         open(run_dir / "transcript.jsonl", encoding="utf-8") if l.strip()]
    return rec


# ================================================================== driver
def main():
    if "--one" in sys.argv:
        name = sys.argv[sys.argv.index("--one") + 1]
        print(json.dumps(run_one(name), ensure_ascii=False, default=str))
        return 0
    results = {}
    for name in SCENARIOS:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--one", name],
                           capture_output=True, text=True, encoding="utf-8",
                           env=dict(os.environ, PYTHONUTF8="1"))
        if p.returncode != 0:
            results[name] = {"error": (p.stderr or p.stdout)[-1500:]}
            print(f"{name:30s} ERROR\n{results[name]['error']}")
            continue
        results[name] = json.loads(p.stdout.strip().splitlines()[-1])
        r = results[name]
        print(f"{name:30s} {len(r['prompts'])} model calls, {len(r['events'])} events, "
              f"{len(r['telegram'])} telegram, {len(r['browser'])} navigations")
    if "--record" in sys.argv:
        json.dump(results, open(sys.argv[sys.argv.index("--record") + 1], "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    if "--compare" in sys.argv:
        base = json.load(open(sys.argv[sys.argv.index("--compare") + 1], encoding="utf-8"))
        bad = 0
        for name in sorted(set(base) | set(results)):
            a, b = base.get(name), results.get(name)
            if a == b:
                continue
            bad += 1
            print(f"DIFFERENT: {name}")
            for part in sorted(set(a or {}) | set(b or {})):
                if (a or {}).get(part) != (b or {}).get(part):
                    xa, xb = (a or {}).get(part), (b or {}).get(part)
                    if isinstance(xa, list) and isinstance(xb, list):
                        for i in range(max(len(xa), len(xb))):
                            ia = xa[i] if i < len(xa) else "<missing>"
                            ib = xb[i] if i < len(xb) else "<missing>"
                            if ia != ib:
                                print(f"   {part}[{i}]:\n     before: {json.dumps(ia, ensure_ascii=False)[:300]}"
                                      f"\n     after:  {json.dumps(ib, ensure_ascii=False)[:300]}")
                                break
                    else:
                        print(f"   {part}: {str(xa)[:200]} -> {str(xb)[:200]}")
        print("IDENTICAL: every scenario" if not bad else f"{bad} scenario(s) differ")
        return 1 if bad else 0
    return 0 if all("error" not in r for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
